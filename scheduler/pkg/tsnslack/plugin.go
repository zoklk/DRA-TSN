// Package tsnslack scores nodes by TSN path slack.
//
// slack = maxLatencyNs - accumulatedLatencyNs (IEEE 802.1Qcc
// user-to-network-requirements/max-latency vs group-status-talker-listener/
// accumulated-latency, both ns). A node's score is the best (max) slack over
// its TX TSNStreams: larger slack = the TSN path to that node has more timing
// headroom. Streams without a latency budget (maxLatencyNs == 0) are ignored.
//
// The plugin is a pure tie-breaker: feasibility is the DRA Filter's job, and
// the profile gives PodTopologySpread weight 100 vs weight 1 here, so slack
// only decides between spread-equivalent candidates (lexicographic order by
// weight separation). Reads TSNStream CRs directly — latency is deliberately
// NOT a ResourceSlice attribute (ADR-0022 identity-only publishing).
//
// Known limitation: the score does not know which stream device the DRA
// allocator will pick for the claim, nor which devices are already allocated;
// it ranks nodes by best-case TX slack.
package tsnslack

import (
	"context"
	"fmt"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/dynamic/dynamicinformer"
	"k8s.io/client-go/tools/cache"
	fwk "k8s.io/kube-scheduler/framework"
	"k8s.io/kubernetes/pkg/scheduler/framework"

	"tsn.dra.net/dratsn/api/v1alpha1"
)

const (
	// Name is the plugin name used in KubeSchedulerConfiguration.
	Name = "TSNSlackScore"

	byNodeIndex      = "spec.nodeName"
	cacheSyncTimeout = 30 * time.Second
)

var tsnstreamGVR = schema.GroupVersionResource{
	Group: "tsn.dra.net", Version: "v1alpha1", Resource: "tsnstreams",
}

// TSNSlack implements framework.ScorePlugin over an informer-backed cache of
// TSNStream CRs indexed by spec.nodeName.
type TSNSlack struct {
	indexer cache.Indexer
}

var _ framework.ScorePlugin = &TSNSlack{}

// New builds the plugin. TSNStream is a CR, so handle.SharedInformerFactory()
// (core resources only) cannot serve it — a dedicated dynamic informer is
// built from the scheduler's kubeconfig instead.
func New(ctx context.Context, _ runtime.Object, h framework.Handle) (framework.Plugin, error) {
	dyn, err := dynamic.NewForConfig(h.KubeConfig())
	if err != nil {
		return nil, fmt.Errorf("tsnslack: dynamic client: %w", err)
	}
	factory := dynamicinformer.NewFilteredDynamicSharedInformerFactory(dyn, 0, metav1.NamespaceAll, nil)
	inf := factory.ForResource(tsnstreamGVR).Informer()
	if err := inf.AddIndexers(cache.Indexers{byNodeIndex: func(obj interface{}) ([]string, error) {
		u, ok := obj.(*unstructured.Unstructured)
		if !ok {
			return nil, nil
		}
		node, _, err := unstructured.NestedString(u.Object, "spec", "nodeName")
		if err != nil || node == "" {
			return nil, nil
		}
		return []string{node}, nil
	}}); err != nil {
		return nil, fmt.Errorf("tsnslack: add index: %w", err)
	}
	factory.Start(ctx.Done())

	syncCtx, cancel := context.WithTimeout(ctx, cacheSyncTimeout)
	defer cancel()
	if !cache.WaitForCacheSync(syncCtx.Done(), inf.HasSynced) {
		return nil, fmt.Errorf("tsnslack: tsnstreams cache did not sync (CRD missing or RBAC denied?)")
	}
	return &TSNSlack{indexer: inf.GetIndexer()}, nil
}

func (p *TSNSlack) Name() string { return Name }

// Score returns the node's best-case TX slack in raw ns; NormalizeScore maps
// the per-node values to 0-100.
func (p *TSNSlack) Score(_ context.Context, _ fwk.CycleState, _ *v1.Pod, nodeInfo fwk.NodeInfo) (int64, *fwk.Status) {
	items, err := p.indexer.ByIndex(byNodeIndex, nodeInfo.Node().Name)
	if err != nil {
		return 0, fwk.AsStatus(err)
	}
	streams := make([]v1alpha1.TSNStream, 0, len(items))
	for _, it := range items {
		u, ok := it.(*unstructured.Unstructured)
		if !ok {
			continue
		}
		var s v1alpha1.TSNStream
		if err := runtime.DefaultUnstructuredConverter.FromUnstructured(u.Object, &s); err != nil {
			return 0, fwk.AsStatus(err)
		}
		streams = append(streams, s)
	}
	return nodeSlack(streams), nil
}

// nodeSlack is the pure scoring function: max over TX streams of
// (maxLatencyNs - accumulatedLatencyNs), clamped at 0. Streams without a
// budget (maxLatencyNs <= 0) and RX streams do not participate.
func nodeSlack(streams []v1alpha1.TSNStream) int64 {
	var best int64
	for _, s := range streams {
		if s.Spec.Direction != v1alpha1.DirectionTX || s.Spec.MaxLatencyNs <= 0 {
			continue
		}
		slack := s.Spec.MaxLatencyNs - s.Spec.AccumulatedLatencyNs
		if slack < 0 {
			slack = 0
		}
		if slack > best {
			best = slack
		}
	}
	return best
}

func (p *TSNSlack) ScoreExtensions() framework.ScoreExtensions { return p }

// NormalizeScore max-normalizes raw ns slack to 0-100. If every candidate is
// zero-slack the scores stay 0 and PodTopologySpread alone decides — the
// correct fallback.
func (p *TSNSlack) NormalizeScore(_ context.Context, _ fwk.CycleState, _ *v1.Pod, scores framework.NodeScoreList) *fwk.Status {
	var max int64
	for _, s := range scores {
		if s.Score > max {
			max = s.Score
		}
	}
	if max == 0 {
		return nil
	}
	for i := range scores {
		scores[i].Score = scores[i].Score * framework.MaxNodeScore / max
	}
	return nil
}
