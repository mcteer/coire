// Binary-only Grafana launcher for Coire's shell-free production image.
package main

import (
	"fmt"
	"os"
	"strings"
	"syscall"
)

func main() {
	const secretVariable = "GF_SECURITY_ADMIN_PASSWORD"
	if path := os.Getenv(secretVariable + "__FILE"); path != "" {
		value, err := os.ReadFile(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "read Grafana admin secret: %v\n", err)
			os.Exit(1)
		}
		if err := os.Setenv(secretVariable, strings.TrimSpace(string(value))); err != nil {
			fmt.Fprintf(os.Stderr, "set Grafana admin secret: %v\n", err)
			os.Exit(1)
		}
		_ = os.Unsetenv(secretVariable + "__FILE")
	}

	binary := "/usr/share/grafana/bin/grafana"
	args := []string{
		binary,
		"server",
		"--homepath=/usr/share/grafana",
		"--config=/etc/grafana/grafana.ini",
		"--packaging=docker",
		"cfg:default.log.mode=console",
		"cfg:default.paths.data=" + envOr("GF_PATHS_DATA", "/tmp/grafana"),
		"cfg:default.paths.logs=" + envOr("GF_PATHS_LOGS", "/tmp/grafana/log"),
		"cfg:default.paths.plugins=" + envOr("GF_PATHS_PLUGINS", "/tmp/grafana/plugins"),
		"cfg:default.paths.provisioning=" + envOr("GF_PATHS_PROVISIONING", "/etc/grafana/provisioning"),
	}
	args = append(args, os.Args[1:]...)
	if err := syscall.Exec(binary, args, os.Environ()); err != nil {
		fmt.Fprintf(os.Stderr, "start Grafana: %v\n", err)
		os.Exit(1)
	}
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
