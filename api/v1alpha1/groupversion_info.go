// Package v1alpha1 contains the DRATSN API types: TSNNic (node TSN-capable NIC
// inventory, published by the DRA plugin) and TSNStream (CNC stream joined to a
// node/NIC, published by the TSN controller). Group tsn.dra.net.
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

// GroupVersion is the API group/version for DRATSN CRDs.
var GroupVersion = schema.GroupVersion{Group: "tsn.dra.net", Version: "v1alpha1"}

// SchemeBuilder registers the DRATSN types with a runtime.Scheme.
var SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

// AddToScheme adds the DRATSN types to a scheme.
var AddToScheme = SchemeBuilder.AddToScheme

func init() {
	SchemeBuilder.Register(&TSNNic{}, &TSNNicList{}, &TSNStream{}, &TSNStreamList{})
}

// objectMeta is a convenience re-export so callers in this package can build
// list/object metadata without importing metav1 directly everywhere.
var _ = metav1.ObjectMeta{}
