package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// TSNNicSpec is the node TSN-capable NIC inventory record published by the DRA
// plugin (NIC discovery) and consumed by the TSN controller (Stream Mapper).
type TSNNicSpec struct {
	// NodeName is the node hosting this NIC.
	NodeName string `json:"nodeName"`
	// IfName is the kernel interface name (e.g. eno5).
	IfName string `json:"ifName"`
	// PCIAddress is the PCI address of the NIC, if known.
	PCIAddress string `json:"pciAddress,omitempty"`
	// MACAddress is the NIC hardware address.
	MACAddress string `json:"macAddress,omitempty"`
	// HostIP is a host IP bound to this NIC; used as the RX receive endpoint.
	HostIP string `json:"hostIP,omitempty"`
	// TSNCapable indicates whether the NIC was determined TSN-capable.
	TSNCapable bool `json:"tsnCapable"`
	// Features lists supported TSN features (e.g. taprio, hw-vlan-insert, ptp).
	Features []string `json:"features,omitempty"`
}

// TSNNicStatus holds observed NIC status.
type TSNNicStatus struct {
	LastProbeTime *metav1.Time       `json:"lastProbeTime,omitempty"`
	Conditions    []metav1.Condition `json:"conditions,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// TSNNic is a cluster-scoped node TSN-capable NIC inventory record.
type TSNNic struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TSNNicSpec   `json:"spec,omitempty"`
	Status TSNNicStatus `json:"status,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// TSNNicList is a list of TSNNic.
type TSNNicList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []TSNNic `json:"items"`
}
