#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Livepatch k8s charm.
"""
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v0.loki_push_api import LogProxyConsumer
from charms.nginx_ingress_integrator.v0.ingress import IngressRequires
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from ops import pebble
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus

import utils
from constants import LOGGER, SCHEMA_UPGRADE_CONTAINER, WORKLOAD_CONTAINER
from state import State

SERVER_PORT = 8080
DATABASE_NAME = "livepatch-server"
LOG_FILE = "/var/log/livepatch"
LOGROTATE_CONFIG_PATH = "/etc/logrotate.d/livepatch"


class LivepatchCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self._state = State(self.app, lambda: self.model.get_relation("livepatch"))

        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.update_status, self.on_update_status)
        self.framework.observe(self.on.leader_elected, self.on_leader_elected)
        self.framework.observe(self.on.start, self.on_start)
        self.framework.observe(self.on.stop, self.on_stop)

        self.framework.observe(self.on.restart_action, self.restart_action)
        self.framework.observe(self.on.schema_upgrade_action, self.schema_upgrade_action)
        self.framework.observe(self.on.schema_version_action, self.schema_version_check_action)

        self.framework.observe(self.on.get_resource_token_action, self.get_resource_token_action)

        # Database
        self.database = DatabaseRequires(
            self,
            relation_name="database",
            database_name=DATABASE_NAME,
        )
        self.framework.observe(self.database.on.database_created, self._on_database_event)
        self.framework.observe(
            self.database.on.endpoints_changed,
            self._on_database_event,
        )
        self.ingress = IngressRequires(
            self,
            {
                "service-hostname": self.config["external_hostname"],
                "service-name": self.app.name,
                "service-port": 8080,
            },
        )

        # Loki log-proxy relation
        self.log_proxy = LogProxyConsumer(
            self,
            log_files=[LOG_FILE],
            relation_name="log-proxy",
            promtail_resource_name="promtail-bin",
            container_name=WORKLOAD_CONTAINER,
        )

        # Prometheus metrics endpoint relation
        self.metrics_endpoint = MetricsEndpointProvider(
            self,
            jobs=[{"static_configs": [{"targets": [f"*:{SERVER_PORT}"]}]}],
            refresh_event=self.on.config_changed,
            relation_name="metrics-endpoint",
        )

        # Grafana dashboard relation
        self._grafana_dashboards = GrafanaDashboardProvider(
            self, relation_name="grafana-dashboard", dashboards_path="./grafana_dashboards"
        )

    # Runs first
    def on_config_changed(self, event):
        self._update_workload_container_config(event)

    # Runs second
    def on_start(self, event):
        self._update_workload_container_config(event)

    # Runs third and on any container restarts & does not guarantee the container is "still up"
    # Runs additionally when; a new unit is created, and upgrade-charm has been run
    # def on_pebble_ready(self, event):
    #    self._update_workload_container_config(event)

    def on_update_status(self, event):
        workload = self.unit.get_container(WORKLOAD_CONTAINER)
        self._ready(workload)

    # Runs AFTER peer-relation-created
    # When a leader loses leadership it only sees the leader-settings-changed
    # As such you will only receive this even if YOU ARE the CURRENT leader (so no need to check)
    def on_leader_elected(self, event):
        self._update_workload_container_config(event)

    def on_stop(self, _):
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if container.can_connect():
            container.stop("livepatch")
            self.unit.status = WaitingStatus("stopped")

    def _update_workload_container_config(self, event):
        """
        Update workload with all available configuration
        data.
        """

        if not self._state.is_ready():
            event.defer()
            LOGGER.warning("State is not ready")
            return

        # Quickly update logrotates config each workload update
        self._push_to_workload(LOGROTATE_CONFIG_PATH, self._get_logrotate_config(), event)

        dsn = self._state.dsn
        if not dsn:
            LOGGER.info("waiting for PG connection string")
            self.unit.status = BlockedStatus("waiting for pg relation.")
            event.defer()
            return
        schema_container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not schema_container.can_connect():
            LOGGER.error("cannot connect to the schema update container")
            self.unit.status = WaitingStatus("Waiting to connect - schema container.")
            event.defer()
            return

        upgrade_required = False
        try:
            upgrade_required = self.migration_is_required(schema_container, dsn)
        except Exception as e:
            LOGGER.error("Failed to determe if schema upgrade required.")
            LOGGER.error(e)
            return
        if upgrade_required:
            if self.unit.is_leader():
                self.schema_upgrade(schema_container, dsn)
            else:
                LOGGER.error("waiting for schema upgrade")
                self.unit.status = WaitingStatus("waiting for schema upgrade")
                event.defer()
                return

        workload_container = self.unit.get_container(WORKLOAD_CONTAINER)

        env_vars = utils.map_config_to_env_vars(self)

        # Some extra config
        env_vars["PATCH_SYNC_TOKEN"] = self._state.resource_token
        env_vars["LP_DATABASE_CONNECTION_STRING"] = dsn
        env_vars["LP_SERVER_SERVER_ADDRESS"] = f":{SERVER_PORT}"
        if self.config.get("patch-sync.enabled") is True:
            # TODO: Find a better way to identify a on-prem syncing instance.
            env_vars["LP_PATCH_SYNC_ID"] = self.model.uuid

        # remove empty environment values
        env_vars = {key: value for key, value in env_vars.items() if value}
        if workload_container.can_connect():
            update_config_environment_layer = {
                "services": {
                    "livepatch": {
                        "summary": "Livepatch Service",
                        "description": "Pebble config layer for livepatch",
                        "override": "merge",
                        "startup": "disabled",
                        "command": "sh -c '/usr/local/bin/livepatch-server | tee {LOG_FILE}'",
                        "environment": env_vars,
                    },
                },
                "checks": {
                    "livepatch-check": {
                        "override": "replace",
                        "period": "1m",
                        "http": {"url": f"http://localhost:{SERVER_PORT}/debug/status"},
                    }
                },
            }

            workload_container.add_layer("livepatch", update_config_environment_layer, combine=True)
            if self._ready(workload_container):
                if workload_container.get_service("livepatch").is_running():
                    workload_container.replan()
                else:
                    workload_container.start("livepatch")
            else:
                self.unit.status = WaitingStatus("Service is not ready")
                return
        else:
            LOGGER.info("workload container not ready - deferring")
            self.unit.status = WaitingStatus("Waiting to connect - workload container")
            event.defer()
            return

        self.unit.status = ActiveStatus()

    def _ready(self, workload_container):
        if workload_container.can_connect():
            plan = workload_container.get_plan()
            if plan.services.get("livepatch") is None:
                LOGGER.info("livepatch service is not ready yet")
                return False
            if workload_container.get_service("livepatch").is_running():
                self.unit.status = ActiveStatus()
            return True
        else:
            LOGGER.error("cannot connect to workload container")
            return False

    # Database

    def _on_database_event(self, event) -> None:
        """Database event handler."""

        if not self.model.unit.is_leader():
            return

        LOGGER.info("(postgresql) RELATION_JOINED event fired.")

        if not self._state.is_ready():
            event.defer()
            LOGGER.warning("State is not ready")
            return

        # get the first endpoint from a comma separate list
        ep = event.endpoints.split(",", 1)[0]
        # compose the db connection string
        uri = f"postgresql://{event.username}:{event.password}@{ep}/{DATABASE_NAME}"

        LOGGER.info("received database uri: {}".format(uri))

        # record the connection string
        self._state.dsn = uri

        self._update_workload(event)

    # Actions
    def restart_action(self, event):
        """
        Restarts the workload container
        """
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if container.can_connect() and container.get_service("livepatch").is_running():
            container.restart()

    def schema_upgrade_action(self, event):
        db_uri = self._get_db_uri()
        container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not db_uri:
            LOGGER.error("DB connection string not set")
            return
        if not container.can_connect():
            LOGGER.error("Cannot connect to the schema update container")
            return
        self.schema_upgrade(container, db_uri)

    def schema_upgrade(self, container, conn_str):
        """
        Performs a schema upgrade on the configurable database
        """
        if not self.unit.is_leader():
            LOGGER.warning("Attempted to run schema upgrade on non-leader unit. Skipping.")
            return

        self.unit.status = WaitingStatus("pg connection successful, attempting upgrade")
        if not container.exists("/usr/local/bin/livepatch-schema-tool"):
            LOGGER.error("livepatch-schema-tool not found in the schema upgrade container")
            self.unit.status = BlockedStatus("Cannot find schema upgrade tool")
            return

        process = None
        try:
            process = container.exec(
                command=[
                    "/usr/local/bin/livepatch-schema-tool",
                    "upgrade",
                    "/etc/livepatch/schema-upgrades",
                    "--db",
                    conn_str,
                ],
            )
        except pebble.APIError as e:
            LOGGER.error(e)
            self.unit.status = BlockedStatus("Schema migration failed")
            return

        try:
            stdout, _ = process.wait_output()
            LOGGER.info(stdout)
            self.unit.status = WaitingStatus("Schema migration done")
        except pebble.ExecError as e:
            LOGGER.error(e)
            LOGGER.error("Exited with code %d. Stderr:", e.exit_code)
            for line in e.stderr.splitlines():
                LOGGER.error("    %s", line)
            self.unit.status = BlockedStatus("Schema migration failed - executing migration failed")
            return

    def schema_version_check_action(self, event) -> str:
        db_uri = self._get_db_uri()
        container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not container.can_connect():
            LOGGER.error("cannot connect to the schema update container")
            return
        self.migration_is_required(container, db_uri)

    def migration_is_required(self, container, conn_str: str) -> bool:
        if not self.unit.is_leader():
            LOGGER.warning("Schema check skipped, can only run on leader.")
            return None

        """Runs a schema version check against the database"""
        if not container.exists("/usr/local/bin/livepatch-schema-tool"):
            LOGGER.error("livepatch-schema-tool not found in the schema upgrade container")
            raise ValueError("Failed to find schema tool")

        if not conn_str:
            LOGGER.error("Database connection string not found")
            raise ValueError("Database connection string is None")

        process = None
        try:
            process = container.exec(
                command=[
                    "/usr/local/bin/livepatch-schema-tool",
                    "check",
                    "/etc/livepatch/schema-upgrades",
                    "--db",
                    conn_str,
                ],
            )
        except pebble.APIError as e:
            LOGGER.error(e)
            raise e

        stdout = None
        try:
            stdout, _ = process.wait_output()
            LOGGER.info("Schema is up to date.")
            LOGGER.info(stdout)
            return False
        except pebble.ExecError as e:
            LOGGER.info(e.stderr)
            if e.exit_code == 2:
                # If command has a non-zero exit code then migrations are pending.
                LOGGER.info("Migrations pending")
                return True
            else:
                # Other exit codes indicate a problem
                raise e

    def get_resource_token_action(self, event):
        """
        Retrieves the livepatch resource token from ua-contracts.
        """
        if not self.unit.is_leader():
            LOGGER.error("cannot fetch the resource token: unit is not the leader")
            event.set_results({"error": "cannot fetch the resource token: unit is not the leader"})
            return

        if not self._state.is_ready():
            LOGGER.error("cannot fetch the resource token: peer relation not ready")
            event.set_results({"error": "cannot fetch the resource token: peer relation not ready"})
            return

        contract_token = event.params.get("contract-token", "")
        proxies = utils.get_proxy_dict(self.config)
        contracts_url = self.config.get("contracts-url", "")
        machine_token = utils.get_machine_token(contract_token, contracts_url=contracts_url, proxies=proxies)

        if not machine_token:
            LOGGER.error("failed to retrieve the machine token")
            event.set_results({"error": "cannot fetch the resource token: failed to fetch the machine token"})
            return

        resource_token = utils.get_resource_token(machine_token, contracts_url=contracts_url, proxies=proxies)

        self._state.resource_token = resource_token

        event.set_results({"result": "resource token set"})

    def set_status_and_log(self, msg, status) -> None:
        """
        A simple wrapper to log and set unit status simultaneously.
        """
        LOGGER.info(msg)
        self.unit.status = status(msg)

    def _get_logrotate_config(self):
        return f"""{LOG_FILE} {"{"}
            rotate 3
            daily
            compress
            delaycompress
            missingok
            notifempty
            size 10M
{"}"}
"""

    def _push_to_workload(self, filename, content, event):
        """Create file on the workload container with
        the specified content."""

        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if container.can_connect():
            LOGGER.info("pushing file {} to the workload container".format(filename))
            container.push(filename, content, make_dirs=True)
        else:
            LOGGER.info("workload container not ready - defering")
            event.defer()
            return


if __name__ == "__main__":
    main(LivepatchCharm, use_juju_for_storage=True)
