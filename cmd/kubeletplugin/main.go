// kubeletplugin is the DRATSN node agent (DaemonSet): NIC discovery (TSNNic),
// ResourceSlice publishing (TSNStream -> device), and the DRA NodePrepare/
// Unprepare handlers that checkpoint pod->stream bindings. Phase 3 adds NRI +
// eBPF on top of the same process/checkpoint.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/dynamic-resource-allocation/kubeletplugin"
	"k8s.io/klog/v2"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"tsn.dra.net/dratsn/api/v1alpha1"
	"tsn.dra.net/dratsn/pkg/checkpoint"
	"tsn.dra.net/dratsn/pkg/dra"
	"tsn.dra.net/dratsn/pkg/ebpf"
	"tsn.dra.net/dratsn/pkg/inventory"
	"tsn.dra.net/dratsn/pkg/nri"
	"tsn.dra.net/dratsn/pkg/resourceslice"
)

func main() {
	var (
		nodeName        = flag.String("node-name", os.Getenv("NODE_NAME"), "this node's name")
		driverName      = flag.String("driver-name", "tsn.dra.net", "DRA driver name")
		capableNics     = flag.String("capable-nics", os.Getenv("TSN_CAPABLE_NICS"), "comma-separated TSN-capable NICs (overrides node label)")
		checkpointPath  = flag.String("checkpoint", "/var/lib/dratsn/checkpoint.db", "BoltDB checkpoint path")
		registrarDir    = flag.String("registrar-dir", "/var/lib/kubelet/plugins_registry", "kubelet plugin registrar dir")
		pluginDir       = flag.String("plugin-dir", "/var/lib/kubelet/plugins/tsn.dra.net", "kubelet plugin data dir")
		pinDir          = flag.String("bpf-pin-dir", ebpf.DefaultPinDir, "bpffs pin dir for TSN maps/links")
		txSNAT          = flag.Bool("tx-snat", os.Getenv("TSN_TX_SNAT") == "true", "SNAT TX: rewrite pod src IP to the egress NIC's own IP before tagging (per-node)")
		snatSourceIP    = flag.String("snat-source-ip", os.Getenv("TSN_SNAT_SOURCE_IP"), "explicit SNAT src IP override (forces this IP instead of the NIC IP; rare, e.g. switchless lab)")
		enableDataplane = flag.Bool("dataplane", true, "load eBPF + NRI data plane (disable for control-plane-only runs)")
		verifyBPF       = flag.Bool("verify-bpf", false, "load eBPF objects (verifier check) and exit")
	)
	klog.InitFlags(nil)
	flag.Parse()
	ctrl.SetLogger(klog.NewKlogr())
	log.SetLogger(klog.NewKlogr())

	if *verifyBPF {
		l, err := ebpf.New(*pinDir)
		if err != nil {
			klog.Fatalf("eBPF verify failed: %v", err)
		}
		l.Close()
		klog.Info("eBPF objects loaded and verified OK")
		return
	}

	if *nodeName == "" {
		klog.Fatal("--node-name (or NODE_NAME) is required")
	}

	ctx := ctrl.SetupSignalHandler()
	cfg := ctrl.GetConfigOrDie()

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: "0",
		LeaderElection:         false,
	})
	if err != nil {
		klog.Fatalf("create manager: %v", err)
	}
	if err := clientgoscheme.AddToScheme(mgr.GetScheme()); err != nil {
		klog.Fatalf("add clientgo scheme: %v", err)
	}
	if err := v1alpha1.AddToScheme(mgr.GetScheme()); err != nil {
		klog.Fatalf("add tsn scheme: %v", err)
	}

	coreClient, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		klog.Fatalf("core client: %v", err)
	}

	// Ensure the kubelet plugin data dir and checkpoint dir exist.
	if err := os.MkdirAll(*pluginDir, 0o755); err != nil {
		klog.Fatalf("create plugin dir %s: %v", *pluginDir, err)
	}
	if err := os.MkdirAll(filepath.Dir(*checkpointPath), 0o755); err != nil {
		klog.Fatalf("create checkpoint dir: %v", err)
	}

	store, err := checkpoint.Open(*checkpointPath)
	if err != nil {
		klog.Fatalf("open checkpoint: %v", err)
	}
	defer store.Close()

	// Data plane: load eBPF + start NRI hooks. NIC (de)attachment is driven by the
	// Node reconciler below, off the capable-nics label.
	var (
		loader    *ebpf.Loader
		nriPlugin *nri.Plugin
	)
	if *enableDataplane {
		loader, err = ebpf.New(*pinDir)
		if err != nil {
			klog.Fatalf("load eBPF data plane: %v", err)
		}
		defer loader.Close()
		nriPlugin, err = nri.New(store, loader, *driverName, *txSNAT, *snatSourceIP)
		if err != nil {
			klog.Fatalf("create NRI plugin: %v", err)
		}
		go nriPlugin.Run(ctx)
		klog.Info("data plane (eBPF + NRI) started")
	}

	// NIC discovery + attachment, level-triggered off the tsn.dra.net/capable-nics
	// node label (ADR-0020). --capable-nics flag/env overrides the label. Changing
	// the label adds/removes TSNNic CRs (stale GC'd) and (de)attaches NIC programs.
	nodeRec := &inventory.NodeReconciler{
		Client:   mgr.GetClient(),
		NodeName: *nodeName,
		Override: inventory.ParseCapableNics(*capableNics),
	}
	if loader != nil {
		nodeRec.Loader = loader
	}
	if err := nodeRec.SetupWithManager(mgr); err != nil {
		klog.Fatalf("setup NIC node reconciler: %v", err)
	}

	// DRA driver + kubelet plugin helper.
	driver := &dra.Driver{
		DriverName: *driverName,
		NodeName:   *nodeName,
		Store:      store,
		GetStream: func(ctx context.Context, name string) (*v1alpha1.TSNStream, error) {
			var s v1alpha1.TSNStream
			if err := mgr.GetClient().Get(ctx, client.ObjectKey{Name: name}, &s); err != nil {
				return nil, err
			}
			return &s, nil
		},
	}

	helper, err := kubeletplugin.Start(ctx, driver,
		kubeletplugin.KubeClient(coreClient),
		kubeletplugin.NodeName(*nodeName),
		kubeletplugin.DriverName(*driverName),
		kubeletplugin.RegistrarDirectoryPath(*registrarDir),
		kubeletplugin.PluginDataDirectoryPath(*pluginDir),
	)
	if err != nil {
		klog.Fatalf("start kubelet plugin: %v", err)
	}
	defer helper.Stop()

	// ResourceSlice republisher: keep devices in sync with this node's TSNStreams.
	if err := mgr.Add(&republisher{
		client:   mgr.GetClient(),
		helper:   helper,
		nodeName: *nodeName,
	}); err != nil {
		klog.Fatalf("add republisher: %v", err)
	}

	// Note: the data plane is programmed only at pod bind/start (NRI RunPodSandbox)
	// and rebuilt from the checkpoint on restart (Synchronize). TSNStream changes are
	// NOT hot-patched into running pods; a change takes effect on pod re-creation
	// (data-plane frozen <=> ResourceClaim allocation frozen). See ADR-0023.
	klog.Infof("DRATSN kubelet plugin starting on node %s (driver %s)", *nodeName, *driverName)
	if err := mgr.Start(ctx); err != nil {
		klog.Fatalf("manager exited: %v", err)
	}
}

// republisher watches this node's TSNStreams and republishes the ResourceSlice
// whenever the set changes.
type republisher struct {
	client   client.Client
	helper   *kubeletplugin.Helper
	nodeName string
	lastHash string
}

func (r *republisher) Start(ctx context.Context) error {
	t := time.NewTicker(3 * time.Second)
	defer t.Stop()
	r.sync(ctx) // initial publish (empty slice advertises liveness)
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			r.sync(ctx)
		}
	}
}

func (r *republisher) sync(ctx context.Context) {
	var list v1alpha1.TSNStreamList
	if err := r.client.List(ctx, &list); err != nil {
		klog.Errorf("republisher list TSNStream: %v", err)
		return
	}
	mine := list.Items[:0]
	for _, s := range list.Items {
		if s.Spec.NodeName == r.nodeName {
			mine = append(mine, s)
		}
	}
	h := hashStreams(mine)
	if h == r.lastHash {
		return
	}
	res := resourceslice.BuildDriverResources(r.nodeName, mine)
	if err := r.helper.PublishResources(ctx, res); err != nil {
		klog.Errorf("publish resources: %v", err)
		return
	}
	r.lastHash = h
	klog.Infof("published ResourceSlice: %d TSN device(s) on node %s", len(mine), r.nodeName)
}

func hashStreams(streams []v1alpha1.TSNStream) string {
	h := ""
	for _, s := range streams {
		h += fmt.Sprintf("%s|%s|%d|%d|%s:%d;", s.Name, s.Spec.Direction, s.Spec.VLANID, s.Spec.PCP, s.Spec.Dst.IP, s.Spec.Dst.Port)
	}
	return h
}
