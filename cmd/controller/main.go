// TSN Controller: a cluster-singleton (leader-elected) Deployment that observes
// the CNC (read-only) and joins streams with node NIC inventory (TSNNic) to emit
// TSNStream CRs. See context/phases/controller.md.
package main

import (
	"flag"
	"os"
	"time"

	"k8s.io/klog/v2"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"tsn.dra.net/dratsn/api/v1alpha1"
	"tsn.dra.net/dratsn/pkg/cnc"
	"tsn.dra.net/dratsn/pkg/controller"
)

func main() {
	var (
		cncURL        = flag.String("cnc-url", "http://mock-cnc.kube-system.svc:8080", "CNC base URL (RESTCONF + SSE)")
		leaderNS      = flag.String("leader-election-namespace", "kube-system", "namespace for the leader election lease")
		resync        = flag.Duration("resync", 30*time.Second, "CNC resync period")
		enableLeaderE = flag.Bool("leader-election", true, "enable leader election (singleton)")
	)
	klog.InitFlags(nil)
	flag.Parse()
	ctrl.SetLogger(klog.NewKlogr())
	log.SetLogger(klog.NewKlogr())

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		LeaderElection:          *enableLeaderE,
		LeaderElectionID:        "tsn-controller.tsn.dra.net",
		LeaderElectionNamespace: *leaderNS,
	})
	if err != nil {
		klog.Fatalf("create manager: %v", err)
	}
	if err := v1alpha1.AddToScheme(mgr.GetScheme()); err != nil {
		klog.Fatalf("add scheme: %v", err)
	}

	mapper := &controller.Mapper{
		Client:     mgr.GetClient(),
		Reader:     mgr.GetAPIReader(), // uncached: avoid stale informer reads
		CNC:        cnc.NewHTTPClient(*cncURL),
		ResyncTime: *resync,
	}
	if err := mgr.Add(mapper); err != nil {
		klog.Fatalf("add mapper: %v", err)
	}

	klog.Info("starting TSN controller manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		klog.Fatalf("manager exited: %v", err)
		os.Exit(1)
	}
}
