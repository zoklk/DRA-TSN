package ebpf

import (
	"errors"
	"net"

	"github.com/cilium/ebpf"
)

func netInterfaceByName(name string) (*net.Interface, error) {
	return net.InterfaceByName(name)
}

// PutTX inserts/updates a tx_flows entry.
func (l *Loader) PutTX(k TXKey, v TXVal) error {
	return l.tx.TxFlows.Update(&k, &v, ebpf.UpdateAny)
}

// DelTX removes a tx_flows entry (missing key is not an error).
func (l *Loader) DelTX(k TXKey) error {
	err := l.tx.TxFlows.Delete(&k)
	if err != nil && !isKeyNotExist(err) {
		return err
	}
	return nil
}

// PutRX inserts/updates an rx_flows entry and the matching devmap entry so the
// XDP redirect target is valid.
func (l *Loader) PutRX(k RXKey, v RXVal) error {
	if err := l.rx.RxFlows.Update(&k, &v, ebpf.UpdateAny); err != nil {
		return err
	}
	return l.PutDevmap(v.VethIfindex)
}

// DelRX removes an rx_flows entry (and leaves the devmap entry; it is harmless
// and shared cleanup happens on veth detach).
func (l *Loader) DelRX(k RXKey) error {
	err := l.rx.RxFlows.Delete(&k)
	if err != nil && !isKeyNotExist(err) {
		return err
	}
	return nil
}

// PutDevmap registers a veth ifindex as a valid XDP redirect target.
func (l *Loader) PutDevmap(ifindex uint32) error {
	return l.rx.XdpDevmap.Update(&ifindex, &ifindex, ebpf.UpdateAny)
}

// DelDevmap removes a devmap redirect target.
func (l *Loader) DelDevmap(ifindex uint32) error {
	err := l.rx.XdpDevmap.Delete(&ifindex)
	if err != nil && !isKeyNotExist(err) {
		return err
	}
	return nil
}

func isKeyNotExist(err error) bool {
	// cilium/ebpf wraps map errors ("delete: %w"), so a bare == never matched
	// and this reported false for every missing key.
	return errors.Is(err, ebpf.ErrKeyNotExist)
}
