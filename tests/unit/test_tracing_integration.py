import dataclasses
from unittest.mock import patch

import pytest
import yaml
from charms.tempo_coordinator_k8s.v0.charm_tracing import charm_tracing_disabled
from charms.tempo_coordinator_k8s.v0.tracing import (
    ProtocolType,
    Receiver,
    TracingProviderAppData,
    TracingRequirerAppData,
    TransportProtocolType,
)
from ops import pebble
from ops.testing import Container, Context, Relation, State

from charm import GrafanaAgentK8sCharm
from grafana_agent import CONFIG_PATH


@pytest.fixture
def ctx():
    with charm_tracing_disabled():
        with patch("socket.getfqdn", new=lambda: "localhost"):
            yield Context(GrafanaAgentK8sCharm)


@pytest.fixture
def base_state():
    yield State(
        leader=True,
        containers=[
            Container(
                "agent",
                can_connect=True,
                # set it to inactive so we can detect when an event has caused it to start
                service_statuses={"agent": pebble.ServiceStatus.INACTIVE},
            )
        ],
    )


def test_tracing_relation(ctx, base_state):
    # GIVEN a tracing relation over the tracing-provider endpoint
    tracing_provider = Relation(
        "tracing-provider",
        remote_app_data=dict(TracingRequirerAppData(receivers=["otlp_http", "otlp_grpc"]).dump()),
    )
    tracing = Relation(
        "tracing",
        remote_app_data=dict(
            TracingProviderAppData(
                receivers=[
                    Receiver(
                        protocol=ProtocolType(
                            name="otlp_grpc", type=TransportProtocolType("grpc")
                        ),
                        url="http:foo.com:1111",
                    )
                ]
            ).dump()
        ),
    )

    state = dataclasses.replace(base_state, relations=[tracing, tracing_provider])
    # WHEN we process any setup event for the relation
    state_out = ctx.run(ctx.on.relation_changed(tracing_provider), state)

    agent = state_out.get_container("agent")

    # THEN the agent has started
    assert agent.services["agent"].is_running()
    # AND the grafana agent config has a traces config section
    fs = agent.get_filesystem(ctx)
    gagent_config = fs.joinpath(*CONFIG_PATH.strip("/").split("/"))
    assert gagent_config.exists()
    yml = yaml.safe_load(gagent_config.read_text())
    assert yml["traces"]["configs"][0], yml.get("traces", "<no traces config>")


def test_tracing_provider_without_tracing(ctx, base_state):
    # GIVEN a tracing relation over the tracing-provider endpoint
    tracing = Relation(
        "tracing-provider",
        remote_app_data=dict(TracingRequirerAppData(receivers=["otlp_http", "otlp_grpc"]).dump()),
    )

    state = dataclasses.replace(base_state, relations=[tracing])
    # WHEN we process any setup event for the relation
    state_out = ctx.run(ctx.on.relation_changed(tracing), state)

    agent = state_out.get_container("agent")

    # THEN the agent has started
    assert agent.services["agent"].is_running()
    # AND the grafana agent config has an empty traces config section
    fs = agent.get_filesystem(ctx)
    gagent_config = fs.joinpath(*CONFIG_PATH.strip("/").split("/"))
    assert gagent_config.exists()
    yml = yaml.safe_load(gagent_config.read_text())
    assert yml["traces"] == {}


def test_tracing_relations_in_and_out(ctx, base_state):
    # GIVEN a tracing relation over the tracing-provider endpoint and one over tracing
    tracing_provider = Relation(
        "tracing-provider",
        remote_app_data=dict(TracingRequirerAppData(receivers=["otlp_http", "otlp_grpc"]).dump()),
    )
    tracing = Relation(
        "tracing",
        remote_app_data=dict(
            TracingProviderAppData(
                receivers=[
                    Receiver(
                        protocol=ProtocolType(
                            name="otlp_grpc", type=TransportProtocolType("grpc")
                        ),
                        url="http:foo.com:1111",
                    )
                ]
            ).dump()
        ),
    )

    state = dataclasses.replace(base_state, relations=[tracing, tracing_provider])
    # WHEN we process any setup event for the relation
    state_out = ctx.run(ctx.on.relation_changed(tracing), state)

    agent = state_out.get_container("agent")

    # THEN the agent has started
    assert agent.services["agent"].is_running()
    # AND the grafana agent config has a traces config section
    fs = agent.get_filesystem(ctx)
    gagent_config = fs.joinpath(*CONFIG_PATH.strip("/").split("/"))
    assert gagent_config.exists()
    yml = yaml.safe_load(gagent_config.read_text())
    assert yml["traces"]


def test_tracing_relation_passthrough(ctx, base_state):
    # GIVEN a tracing relation over the tracing-provider endpoint and one over tracing
    tracing_provider = Relation(
        "tracing-provider",
        remote_app_data=dict(TracingRequirerAppData(receivers=["otlp_http", "otlp_grpc"]).dump()),
    )
    tracing = Relation(
        "tracing",
        remote_app_data=dict(
            TracingProviderAppData(
                receivers=[
                    Receiver(
                        protocol=ProtocolType(
                            name="otlp_grpc", type=TransportProtocolType("grpc")
                        ),
                        url="http:foo.com:1111",
                    )
                ]
            ).dump()
        ),
    )

    state = dataclasses.replace(base_state, relations=[tracing, tracing_provider])
    # WHEN we process any setup event for the relation
    state_out = ctx.run(ctx.on.relation_changed(tracing), state)

    # THEN we act as a tracing provider for 'tracing-provider', and as requirer for 'tracing'
    tracing_out = TracingRequirerAppData.load(state_out.get_relations("tracing")[0].local_app_data)
    tracing_provider_out = TracingProviderAppData.load(
        state_out.get_relations("tracing-provider")[0].local_app_data
    )
    assert set(tracing_out.receivers) == {"otlp_grpc", "otlp_http"}
    otlp_grpc_provider_def = [
        r for r in tracing_provider_out.receivers if r.protocol.name == "otlp_grpc"
    ][0]
    otlp_http_provider_def = [
        r for r in tracing_provider_out.receivers if r.protocol.name == "otlp_http"
    ][0]
    assert otlp_grpc_provider_def.url == "localhost:4317"
    assert otlp_http_provider_def.url == "http://localhost:4318"


@pytest.mark.parametrize(
    "force_enable",
    (
        ["zipkin", "jaeger_thrift_http", "jaeger_grpc"],
        ["zipkin", "jaeger_thrift_http"],
        ["jaeger_thrift_http"],
    ),
)
def test_tracing_relation_passthrough_with_force_enable(ctx, base_state, force_enable):
    # GIVEN a tracing relation over the tracing-provider endpoint and one over tracing
    tracing_provider = Relation(
        "tracing-provider",
        remote_app_data=dict(TracingRequirerAppData(receivers=["otlp_http", "otlp_grpc"]).dump()),
    )
    tracing = Relation(
        "tracing",
        remote_app_data=dict(
            TracingProviderAppData(
                receivers=[
                    Receiver(
                        protocol=ProtocolType(
                            name="otlp_grpc", type=TransportProtocolType("grpc")
                        ),
                        url="http:foo.com:1111",
                    )
                ]
            ).dump()
        ),
    )

    # AND given we're configured to always enable some protocols
    state = dataclasses.replace(
        base_state,
        config={f"always_enable_{proto}": True for proto in force_enable},
        relations=[tracing, tracing_provider],
    )
    # WHEN we process any setup event for the relation
    state_out = ctx.run(ctx.on.relation_changed(tracing), state)

    # THEN we act as a tracing provider for 'tracing-provider', and as requirer for 'tracing'
    tracing_out = TracingRequirerAppData.load(state_out.get_relations("tracing")[0].local_app_data)
    tracing_provider_out = TracingProviderAppData.load(
        state_out.get_relations("tracing-provider")[0].local_app_data
    )

    # we still only request otlp grpc and http for charm traces and because gagent funnels all to grpc
    assert set(tracing_out.receivers) == {"otlp_grpc", "otlp_http"}
    # but we provide all
    providing_protocols = {r.protocol.name for r in tracing_provider_out.receivers}
    assert providing_protocols == {"otlp_grpc", "otlp_http"}.union(force_enable)


@pytest.mark.parametrize(
    "sampling_config",
    (
        {},
        {
            "tracing_sample_rate_charm": 23.0,
            "tracing_sample_rate_workload": 13.13,
            "tracing_sample_rate_error": 42.42,
        },
    ),
)
def test_tracing_sampling_config_is_present(ctx, base_state, sampling_config):
    # GIVEN a tracing relation over the tracing-provider endpoint and one over tracing
    tracing_provider = Relation(
        "tracing-provider",
        remote_app_data=dict(TracingRequirerAppData(receivers=["otlp_http", "otlp_grpc"]).dump()),
    )
    tracing = Relation(
        "tracing",
        remote_app_data=dict(
            TracingProviderAppData(
                receivers=[
                    Receiver(
                        protocol=ProtocolType(
                            name="otlp_grpc", type=TransportProtocolType("grpc")
                        ),
                        url="http:foo.com:1111",
                    )
                ]
            ).dump()
        ),
    )

    state = dataclasses.replace(
        base_state, relations=[tracing, tracing_provider], config=sampling_config
    )
    # WHEN we process any setup event for the relation
    state_out = ctx.run(ctx.on.relation_changed(tracing), state)

    agent = state_out.get_container("agent")

    # THEN the grafana agent config has a traces tail_sampling section with default values
    fs = agent.get_filesystem(ctx)
    gagent_config = fs.joinpath(*CONFIG_PATH.strip("/").split("/"))
    assert gagent_config.exists()
    yml = yaml.safe_load(gagent_config.read_text())

    assert yml["traces"]["configs"][0]["tail_sampling"]
