// tsn-scheduler is the upstream kube-scheduler with the TSNSlackScore plugin
// registered (scheduling framework out-of-tree extension). It runs as a second
// scheduler: only pods with schedulerName: tsn-scheduler are handled, the
// default scheduler stays untouched. Profile (score weights, plugin set) comes
// from the KubeSchedulerConfiguration passed via --config.
package main

import (
	"os"

	"k8s.io/component-base/cli"
	"k8s.io/kubernetes/cmd/kube-scheduler/app"

	"tsn.dra.net/dratsn/scheduler/pkg/tsnslack"
)

func main() {
	command := app.NewSchedulerCommand(
		app.WithPlugin(tsnslack.Name, tsnslack.New),
	)
	os.Exit(cli.Run(command))
}
