// Package checkpoint persists the pod->stream binding that couples Phase 2 (DRA
// NodePrepareResources, which knows the claim->pod->stream mapping) with Phase 3
// (NRI hooks, which need that mapping at pod start to program bpf maps and inject
// env). It uses BoltDB so the binding survives a driver restart (NRI Synchronize
// rebuilds map state from it).
package checkpoint

import (
	"encoding/json"
	"fmt"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	podsBucket   = []byte("pods")
	claimsBucket = []byte("claims") // claimUID -> JSON []podUID, for DRA Unprepare
)

// StreamConfig is the per-stream data the NRI hooks consume. It is the projection
// of a TSNStream allocated to a pod via a ResourceClaim. One pod may have several
// (e.g. a TX and an RX stream).
type StreamConfig struct {
	StreamID  string `json:"streamId"`
	Direction string `json:"direction"` // TX | RX
	IfName    string `json:"ifName"`    // TSN-capable NIC to attach/program
	VLANID    uint16 `json:"vlanId"`
	PCP       uint8  `json:"pcp"`
	// DstIP/DstPort/DstMAC: TX -> external listener; RX -> this node's NIC endpoint.
	DstIP   string `json:"dstIp"`
	DstPort uint16 `json:"dstPort"`
	DstMAC  string `json:"dstMac,omitempty"`
	// TalkerIP/TalkerMAC identify the stream sender (RX stream-id hardening, ADR-0027).
	// RX: the sensor source — TalkerIP is required (it is part of the rx_flows key);
	// TalkerMAC is optional (enforced as an L2 check only when present).
	TalkerIP  string `json:"talkerIp,omitempty"`
	TalkerMAC string `json:"talkerMac,omitempty"`
}

// PodRecord is the value stored per pod UID.
type PodRecord struct {
	PodUID    string         `json:"podUid"`
	Namespace string         `json:"namespace,omitempty"`
	Name      string         `json:"name,omitempty"`
	Streams   []StreamConfig `json:"streams"`
	// Runtime-resolved fields filled by the NRI hooks at pod start; kept so map
	// entries can be removed/rebuilt at pod stop or on a live TSNStream update
	// without re-entering the (gone) netns.
	PodIP           string    `json:"podIp,omitempty"`
	PodMAC          string    `json:"podMac,omitempty"`
	HostVethIfindex int       `json:"hostVethIfindex,omitempty"`
	UpdatedAt       time.Time `json:"updatedAt"`
}

// Store is a BoltDB-backed pod->stream checkpoint.
type Store struct {
	db *bolt.DB
}

// Open opens (creating if needed) the checkpoint at path.
func Open(path string) (*Store, error) {
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 3 * time.Second})
	if err != nil {
		return nil, fmt.Errorf("open checkpoint %s: %w", path, err)
	}
	err = db.Update(func(tx *bolt.Tx) error {
		if _, e := tx.CreateBucketIfNotExists(podsBucket); e != nil {
			return e
		}
		_, e := tx.CreateBucketIfNotExists(claimsBucket)
		return e
	})
	if err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

// PutPod stores (overwrites) the stream config for a pod UID.
func (s *Store) PutPod(rec PodRecord) error {
	rec.UpdatedAt = time.Now()
	buf, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(podsBucket).Put([]byte(rec.PodUID), buf)
	})
}

// GetPod returns the record for a pod UID, ok=false if absent.
func (s *Store) GetPod(uid string) (PodRecord, bool, error) {
	var rec PodRecord
	found := false
	err := s.db.View(func(tx *bolt.Tx) error {
		v := tx.Bucket(podsBucket).Get([]byte(uid))
		if v == nil {
			return nil
		}
		found = true
		return json.Unmarshal(v, &rec)
	})
	return rec, found, err
}

// DeletePod removes a pod UID's record.
func (s *Store) DeletePod(uid string) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(podsBucket).Delete([]byte(uid))
	})
}

// PutClaimPods records which pod UIDs a claim was prepared for, so DRA
// Unprepare (which only gets the claim) can find the pod records to remove.
func (s *Store) PutClaimPods(claimUID string, podUIDs []string) error {
	buf, err := json.Marshal(podUIDs)
	if err != nil {
		return err
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(claimsBucket).Put([]byte(claimUID), buf)
	})
}

// TakeClaimPods returns and deletes the pod UIDs recorded for a claim.
func (s *Store) TakeClaimPods(claimUID string) ([]string, error) {
	var pods []string
	err := s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(claimsBucket)
		v := b.Get([]byte(claimUID))
		if v == nil {
			return nil
		}
		if err := json.Unmarshal(v, &pods); err != nil {
			return err
		}
		return b.Delete([]byte(claimUID))
	})
	return pods, err
}

// ListPods returns all stored pod records (used by NRI Synchronize on restart).
func (s *Store) ListPods() ([]PodRecord, error) {
	var out []PodRecord
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(podsBucket).ForEach(func(_, v []byte) error {
			var rec PodRecord
			if err := json.Unmarshal(v, &rec); err != nil {
				return err
			}
			out = append(out, rec)
			return nil
		})
	})
	return out, err
}
