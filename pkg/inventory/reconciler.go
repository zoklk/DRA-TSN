package inventory

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/klog/v2"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	"tsn.dra.net/dratsn/api/v1alpha1"
	"tsn.dra.net/dratsn/pkg/ebpf"
)

// NicAttacher is the subset of *ebpf.Loader the reconciler uses to (de)attach the
// TSN programs as NICs enter/leave the capable-nics label. Nil = data plane off.
type NicAttacher interface {
	AttachNIC(ifName string) error
	DetachNIC(ifName string) error
}

// NodeReconciler keeps this node's TSNNic CRs and eBPF NIC attachments in sync
// with the tsn.dra.net/capable-nics node label (level-triggered, ADR-0020). It
// replaces the old start-once label read: changing the label now adds/removes
// TSNNic CRs (stale ones are GC'd) and attaches/detaches the NIC programs.
type NodeReconciler struct {
	Client   client.Client
	NodeName string
	// Loader attaches/detaches the per-NIC programs; nil when the data plane is off.
	Loader NicAttacher
	// Override, if non-empty, wins over the node label (the --capable-nics flag/env).
	Override []string

	attached map[string]bool
}

// Reconcile recomputes the desired NIC set from the label (or override) and
// converges TSNNic CRs + NIC attachments to it.
func (r *NodeReconciler) Reconcile(ctx context.Context, _ reconcile.Request) (ctrl.Result, error) {
	desired := r.Override
	if len(desired) == 0 {
		var node corev1.Node
		if err := r.Client.Get(ctx, client.ObjectKey{Name: r.NodeName}, &node); err != nil {
			return ctrl.Result{}, client.IgnoreNotFound(err)
		}
		desired = ParseCapableNics(node.Labels[CapableNicsLabel])
	}

	// Desired TSNNic specs (only NICs that actually exist on this node).
	nics := Discover(desired)
	want := make(map[string]v1alpha1.TSNNicSpec, len(nics))
	wantIf := make(map[string]bool, len(nics))
	for _, nic := range nics {
		name := fmt.Sprintf("%s-%s", r.NodeName, nic.IfName)
		want[name] = v1alpha1.TSNNicSpec{
			NodeName:   r.NodeName,
			IfName:     nic.IfName,
			PCIAddress: nic.PCIAddress,
			MACAddress: nic.MACAddress,
			HostIP:     nic.HostIP,
			TSNCapable: true,
			Features:   []string{"label-declared"},
		}
		wantIf[nic.IfName] = true
	}

	// Converge TSNNic CRs for this node: update changed, GC stale.
	var list v1alpha1.TSNNicList
	if err := r.Client.List(ctx, &list); err != nil {
		return ctrl.Result{}, fmt.Errorf("list TSNNic: %w", err)
	}
	have := make(map[string]bool)
	for i := range list.Items {
		cur := &list.Items[i]
		if cur.Spec.NodeName != r.NodeName {
			continue
		}
		spec, ok := want[cur.Name]
		if !ok {
			if err := r.Client.Delete(ctx, cur); err != nil {
				klog.Errorf("GC stale TSNNic %s: %v", cur.Name, err)
			} else {
				klog.Infof("GC'd stale TSNNic %s (NIC left capable-nics label)", cur.Name)
			}
			continue
		}
		have[cur.Name] = true
		if !nicSpecEqual(cur.Spec, spec) {
			cur.Spec = spec
			if err := r.Client.Update(ctx, cur); err != nil {
				klog.Errorf("update TSNNic %s: %v", cur.Name, err)
			}
		}
	}
	for name, spec := range want {
		if have[name] {
			continue
		}
		nic := &v1alpha1.TSNNic{ObjectMeta: metav1.ObjectMeta{Name: name}, Spec: spec}
		if err := r.Client.Create(ctx, nic); err != nil {
			klog.Errorf("create TSNNic %s: %v", name, err)
		} else {
			klog.Infof("published TSNNic %s (mac %s hostIP %s)", name, spec.MACAddress, spec.HostIP)
		}
	}

	// Converge eBPF NIC attachments to the desired interface set.
	if r.Loader != nil {
		for ifn := range wantIf {
			if r.attached[ifn] {
				continue
			}
			if err := r.Loader.AttachNIC(ifn); err != nil {
				klog.Errorf("attach TSN NIC %s: %v", ifn, err)
				continue
			}
			r.attached[ifn] = true
		}
		for ifn := range r.attached {
			if wantIf[ifn] {
				continue
			}
			if err := r.Loader.DetachNIC(ifn); err != nil {
				klog.Errorf("detach TSN NIC %s: %v", ifn, err)
			}
			delete(r.attached, ifn)
		}
	}
	return ctrl.Result{}, nil
}

// SetupWithManager registers the reconciler, watching only this node's Node object.
func (r *NodeReconciler) SetupWithManager(mgr ctrl.Manager) error {
	r.attached = map[string]bool{}
	ownNode := predicate.NewPredicateFuncs(func(o client.Object) bool {
		return o.GetName() == r.NodeName
	})
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Node{}, builder.WithPredicates(ownNode)).
		Named("tsnnic-node").
		Complete(r)
}

// compile-time assertion that the real loader satisfies NicAttacher.
var _ NicAttacher = (*ebpf.Loader)(nil)
