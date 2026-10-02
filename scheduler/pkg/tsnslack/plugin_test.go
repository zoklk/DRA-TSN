package tsnslack

import (
	"context"
	"testing"

	"k8s.io/kubernetes/pkg/scheduler/framework"

	"tsn.dra.net/dratsn/api/v1alpha1"
)

func stream(dir v1alpha1.StreamDirection, maxNs, accNs int64) v1alpha1.TSNStream {
	return v1alpha1.TSNStream{Spec: v1alpha1.TSNStreamSpec{
		Direction: dir, MaxLatencyNs: maxNs, AccumulatedLatencyNs: accNs,
	}}
}

func TestNodeSlack(t *testing.T) {
	cases := []struct {
		name    string
		streams []v1alpha1.TSNStream
		want    int64
	}{
		{"no streams", nil, 0},
		{"single TX", []v1alpha1.TSNStream{stream(v1alpha1.DirectionTX, 1000000, 250000)}, 750000},
		{"max of several", []v1alpha1.TSNStream{
			stream(v1alpha1.DirectionTX, 1000000, 400000),
			stream(v1alpha1.DirectionTX, 1000000, 100000),
		}, 900000},
		{"RX ignored", []v1alpha1.TSNStream{stream(v1alpha1.DirectionRX, 1000000, 0)}, 0},
		{"no budget ignored", []v1alpha1.TSNStream{stream(v1alpha1.DirectionTX, 0, 0)}, 0},
		{"accumulated over budget clamps to 0", []v1alpha1.TSNStream{
			stream(v1alpha1.DirectionTX, 1000000, 1500000),
		}, 0},
	}
	for _, c := range cases {
		if got := nodeSlack(c.streams); got != c.want {
			t.Errorf("%s: nodeSlack = %d, want %d", c.name, got, c.want)
		}
	}
}

func TestNormalizeScore(t *testing.T) {
	p := &TSNSlack{}

	scores := framework.NodeScoreList{
		{Name: "a", Score: 300000}, {Name: "b", Score: 100000}, {Name: "c", Score: 0},
	}
	if st := p.NormalizeScore(context.Background(), nil, nil, scores); st != nil {
		t.Fatalf("NormalizeScore status = %v", st)
	}
	if scores[0].Score != 100 || scores[1].Score != 33 || scores[2].Score != 0 {
		t.Errorf("normalized = %v, want [100 33 0]", scores)
	}

	// All-zero must stay all-zero (spread alone decides), not divide by zero.
	zero := framework.NodeScoreList{{Name: "a"}, {Name: "b"}}
	if st := p.NormalizeScore(context.Background(), nil, nil, zero); st != nil {
		t.Fatalf("NormalizeScore(all-zero) status = %v", st)
	}
	if zero[0].Score != 0 || zero[1].Score != 0 {
		t.Errorf("all-zero normalized = %v, want zeros", zero)
	}
}
