# DRATSN build. go 1.25 is fetched automatically via GOTOOLCHAIN=auto.
SHELL := /bin/bash
export GOTOOLCHAIN := auto

# Local registry (set REGISTRY to the cluster registry, e.g. 192.168.110.11:5000)
REGISTRY ?= localhost:5000
TAG      ?= dev
BINDIR   := bin

CMDS := controller mock-cnc kubeletplugin

.PHONY: all build generate bpf vmlinux image push clean test fmt vet

all: generate build

## build: compile all Go binaries
build:
	@mkdir -p $(BINDIR)
	@for c in $(CMDS); do \
		echo "build $$c"; \
		CGO_ENABLED=0 go build -o $(BINDIR)/$$c ./cmd/$$c || exit 1; \
	done
	@echo "build tsn-scheduler"
	@cd scheduler && CGO_ENABLED=0 go build -o ../$(BINDIR)/tsn-scheduler ./cmd/tsn-scheduler

## generate: regenerate bpf2go objects (deepcopy + CRDs are hand-maintained)
generate: bpf

## vmlinux: regenerate bpf/headers/vmlinux.h from the running kernel BTF
vmlinux:
	bpftool btf dump file /sys/kernel/btf/vmlinux format c > bpf/headers/vmlinux.h

## bpf: compile eBPF C -> Go via bpf2go (requires clang + bpf2go on PATH)
bpf:
	@command -v bpf2go >/dev/null || go install github.com/cilium/ebpf/cmd/bpf2go@v0.21.0
	go generate ./pkg/ebpf/...

test:
	go test ./... 2>&1
	cd scheduler && go test ./... 2>&1

fmt:
	go fmt ./...

vet:
	go vet ./...

## image: build container images for all components
image:
	docker build -f deployments/Dockerfile.controller -t $(REGISTRY)/dratsn-controller:$(TAG) .
	docker build -f deployments/Dockerfile.mock-cnc   -t $(REGISTRY)/dratsn-mock-cnc:$(TAG) .
	docker build -f deployments/Dockerfile.kubeletplugin -t $(REGISTRY)/dratsn-driver:$(TAG) .
	docker build -f deployments/Dockerfile.tsn-scheduler -t $(REGISTRY)/dratsn-tsn-scheduler:$(TAG) .

## push: push images to the registry
push:
	docker push $(REGISTRY)/dratsn-controller:$(TAG)
	docker push $(REGISTRY)/dratsn-mock-cnc:$(TAG)
	docker push $(REGISTRY)/dratsn-driver:$(TAG)
	docker push $(REGISTRY)/dratsn-tsn-scheduler:$(TAG)

clean:
	rm -rf $(BINDIR)
