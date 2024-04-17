#!/usr/bin/env python3

# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""A  juju charm for Grafana Agent on Kubernetes."""
import json
import logging
import pathlib
from typing import Any, Dict, List, Optional, Union

import yaml
from charms.loki_k8s.v1.loki_push_api import LokiPushApiProvider
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointConsumer
from charms.tempo_k8s.v1.charm_tracing import trace_charm
from charms.tempo_k8s.v2.tracing import TracingEndpointRequirer
from cosl import GrafanaDashboard
from grafana_agent import CONFIG_PATH, GrafanaAgentCharm
from ops.main import main
from ops.pebble import Layer

logger = logging.getLogger(__name__)

SCRAPE_RELATION_NAME = "metrics-endpoint"


@trace_charm(
    tracing_endpoint="tracing_endpoint",
    server_cert="server_cert_path",
    extra_types=(
        GrafanaAgentCharm,
        LokiPushApiProvider,
        MetricsEndpointConsumer,
        GrafanaDashboard,
    ),
)
class GrafanaAgentK8sCharm(GrafanaAgentCharm):
    """K8s version of the Grafana Agent charm."""

    mandatory_relation_pairs = {
        "metrics-endpoint": [  # must be paired with:
            {"send-remote-write"},  # or
            {"grafana-cloud-config"},
        ],
        "logging-provider": [  # must be paired with:
            {"logging-consumer"},  # or
            {"grafana-cloud-config"},
        ],
        "grafana-dashboards-consumer": [  # must be paired with:
            {"grafana-dashboards-provider"},  # or
            {"grafana-cloud-config"},
        ],
    }

    def __init__(self, *args):
        super().__init__(*args)
        self._container = self.unit.get_container(self._name)
        self.unit.set_ports(self._http_listen_port, self._grpc_listen_port)

        self._scrape = MetricsEndpointConsumer(self)
        self.framework.observe(
            self._scrape.on.targets_changed,  # pyright: ignore
            self.on_scrape_targets_changed,
        )

        self._loki_provider = LokiPushApiProvider(
            self, relation_name="logging-provider", port=self._http_listen_port
        )

        self.framework.observe(
            self._loki_provider.on.loki_push_api_alert_rules_changed,  # pyright: ignore
            self._on_loki_push_api_alert_rules_changed,
        )

        self._tracing = TracingEndpointRequirer(self, protocols=["otlp_http"])
        self.framework.observe(
            self.on["grafana-dashboards-consumer"].relation_changed,
            self._on_dashboards_changed,
        )
        self.framework.observe(
            self.on["grafana-dashboards-consumer"].relation_broken,
            self._on_dashboards_changed,
        )

        self.framework.observe(
            self.on.agent_pebble_ready,  # pyright: ignore
            self._on_agent_pebble_ready,
        )

    def _on_loki_push_api_alert_rules_changed(self, _event):
        """Refresh Loki alert rules."""
        self._update_loki_alerts()

    def _layer(self) -> Layer:
        """Generate the pebble layer."""
        return Layer(
            {
                "summary": "agent layer",
                "description": "pebble config layer for Grafana Agent",
                "services": {
                    "agent": {
                        "override": "replace",
                        "summary": "agent",
                        "command": f"/bin/agent {self._cli_args()}",
                        "startup": "enabled",
                    },
                },
            },
        )

    def _on_dashboards_changed(self, _event) -> None:
        logger.info("updating dashboards")

        if not self.unit.is_leader():
            return

        self.update_dashboards(
            dashboards=self.dashboards,
            reload_func=self._grafana_dashboards_provider._update_all_dashboards_from_dir,
            mapping=self.dashboard_paths,
        )

    def _on_agent_pebble_ready(self, _event) -> None:
        self._container.push(CONFIG_PATH, yaml.dump(self._generate_config()), make_dirs=True)

        self._container.add_layer(self._name, self._layer(), combine=True)
        self._container.autostart()

        if (version := self._agent_version) is not None:
            self.unit.set_workload_version(version)
        else:
            logger.debug(
                "Cannot set workload version at this time: could not get grafana-agent version."
            )
        self._update_status()

    def metrics_rules(self) -> Dict[str, Any]:
        """Return a list of metrics rules."""
        return self._scrape.alerts

    @property
    def dashboards(self) -> list:
        """Returns an aggregate of all dashboards received by this grafana-agent."""
        aggregate = {}
        for rel in self.model.relations["grafana-dashboards-consumer"]:
            dashboards = json.loads(rel.data[rel.app].get("dashboards", "{}"))  # type: ignore
            if "templates" not in dashboards:
                continue
            for template in dashboards["templates"]:
                content = GrafanaDashboard(
                    dashboards["templates"][template].get("content")
                )._deserialize()
                entry = {
                    "charm": dashboards["templates"][template].get("charm", "charm_name"),
                    "relation_id": rel.id,
                    "title": template,
                    "content": content,
                }
                aggregate[template] = entry

        return list(aggregate.values())

    def metrics_jobs(self) -> list:
        """Return a list of metrics scrape jobs."""
        return self._scrape.jobs()

    def logs_rules(self) -> Dict[str, Any]:
        """Return a list of logging rules."""
        return self._loki_provider.alerts

    @property
    def is_k8s(self) -> bool:
        """Is this a k8s charm."""
        return True

    @property
    def is_ready(self):
        """Checks if the charm is ready for configuration."""
        return self._container.can_connect()

    @property
    def _additional_integrations(self) -> Dict:
        """No additions for k8s charms."""
        return {}

    @property
    def _additional_log_configs(self) -> List[Dict[str, Any]]:
        """Additional per-type integrations to inject."""
        return []

    def agent_version_output(self) -> str:
        """Runs `agent -version` and returns the output.

        Returns:
            Output of `agent -version`
        """
        version_output, _ = self._container.exec(["/bin/agent", "-version"]).wait_output()
        return version_output

    def read_file(self, filepath: Union[str, pathlib.Path]):
        """Read a file's contents.

        Returns:
            A string with the file's contents
        """
        return self._container.pull(filepath).read()

    def write_file(self, path: Union[str, pathlib.Path], text: str) -> None:
        """Write text to a file.

        Args:
            path: file path to write to
            text: text to write to the file
        """
        self._container.push(path, text, make_dirs=True)

    def delete_file(self, path: Union[str, pathlib.Path]):
        """Delete a file.

        Args:
            path: file to be deleted
        """
        self._container.remove_path(path)

    def stop(self) -> None:
        """Stop grafana agent."""
        self._container.stop("agent")

    def restart(self) -> None:
        """Restart grafana agent."""
        self._container.add_layer(self._name, self._layer(), combine=True)
        self._container.autostart()
        self._container.restart("agent")

    def positions_dir(self) -> str:
        """Return the positions directory."""
        return "/run"

    def run(self, cmd: List[str]):
        """Run cmd on the workload.

        Args:
            cmd: Command to be run.
        """
        self._container.exec(cmd).wait()

    @property
    def tracing_endpoint(self) -> Optional[str]:
        """Otlp http endpoint for charm instrumentation."""
        if self._tracing.is_ready():
            return self._tracing.get_endpoint("otlp_http")
        return None

    @property
    def server_cert_path(self) -> Optional[str]:
        """Server certificate path for tls tracing."""
        return self._cert_path


if __name__ == "__main__":
    main(GrafanaAgentK8sCharm)
