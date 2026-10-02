package checkpoint

import (
	"path/filepath"
	"testing"
)

func openTemp(t *testing.T) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "checkpoint.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestPodRoundtrip(t *testing.T) {
	s := openTemp(t)
	rec := PodRecord{
		PodUID:    "uid-1",
		Namespace: "dratsn-test",
		Name:      "vplc-e2e",
		Streams: []StreamConfig{
			{StreamID: "s-tx", Direction: "TX", IfName: "eno6", VLANID: 200, PCP: 6, DstIP: "192.168.50.10", DstPort: 4840, DstMAC: "02:00:00:00:50:10"},
		},
		PodIP:           "10.244.1.5",
		PodMAC:          "4a:bc:76:c7:d1:98",
		HostVethIfindex: 8339,
	}
	if err := s.PutPod(rec); err != nil {
		t.Fatalf("PutPod: %v", err)
	}

	got, ok, err := s.GetPod("uid-1")
	if err != nil || !ok {
		t.Fatalf("GetPod: ok=%v err=%v", ok, err)
	}
	// Runtime fields (incl PodMAC, used to rebuild RX val on live update) must persist.
	if got.PodMAC != "4a:bc:76:c7:d1:98" || got.HostVethIfindex != 8339 || got.PodIP != "10.244.1.5" {
		t.Errorf("runtime fields not preserved: %+v", got)
	}
	if len(got.Streams) != 1 || got.Streams[0].VLANID != 200 || got.Streams[0].DstMAC != "02:00:00:00:50:10" {
		t.Errorf("stream not preserved: %+v", got.Streams)
	}
}

func TestGetMissing(t *testing.T) {
	s := openTemp(t)
	if _, ok, err := s.GetPod("nope"); ok || err != nil {
		t.Errorf("GetPod(missing) = ok %v err %v, want false,nil", ok, err)
	}
}

func TestDeleteAndList(t *testing.T) {
	s := openTemp(t)
	for _, uid := range []string{"a", "b"} {
		if err := s.PutPod(PodRecord{PodUID: uid}); err != nil {
			t.Fatalf("PutPod %s: %v", uid, err)
		}
	}
	if list, err := s.ListPods(); err != nil || len(list) != 2 {
		t.Fatalf("ListPods = %d (err %v), want 2", len(list), err)
	}
	if err := s.DeletePod("a"); err != nil {
		t.Fatalf("DeletePod: %v", err)
	}
	list, _ := s.ListPods()
	if len(list) != 1 || list[0].PodUID != "b" {
		t.Errorf("after delete, list = %+v, want [b]", list)
	}
	// Deleting a missing pod is not an error.
	if err := s.DeletePod("a"); err != nil {
		t.Errorf("DeletePod(missing) = %v, want nil", err)
	}
}
