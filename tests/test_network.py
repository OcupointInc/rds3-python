import pytest

import rds3.network as network
from rds3 import NetworkSetupError, discover, set_network
from fake_discovery import FakeDiscovery

LOOP = dict(interface_ip="127.0.0.1", address="127.0.0.1")


@pytest.fixture
def board(monkeypatch):
    monkeypatch.setattr(network, "_REBOOT_S", 0.0)
    b = FakeDiscovery()
    yield b
    b.close()


def test_discover_lists_rds3s_with_their_network_settings(board):
    other = FakeDiscovery(board="rf_switch", mac=bytes.fromhex("62eb988a0f01"))
    try:
        for port, expect in ((board.port, 1), (other.port, 0)):
            found = discover(0.5, port=port, **LOOP)
            assert len(found) == expect
        d = discover(0.5, port=board.port, **LOOP)[0]
        assert (d.mac, d.ip, d.subnet, d.gateway) == ("7a:c5:ba:d8:51:d6", "169.254.81.214", "255.255.0.0", "0.0.0.0")
        assert d.dhcp and d.dhcp_fallback and d.network_setup and d.board == "rds3"
        assert "169.254.81.214/16" in str(d) and "fallback" in str(d)
        assert discover(0.5, port=other.port, all_boards=True, **LOOP)[0].board == "rf_switch"
    finally:
        other.close()


def test_set_static_address_and_wait_for_the_board(board):
    d = set_network("7A-C5-BA-D8-51-D6", ip="10.20.30.40", subnet=24, gateway="10.20.30.1", port=board.port, **LOOP)
    assert (d.ip, d.subnet, d.gateway, d.dhcp) == ("10.20.30.40", "255.255.255.0", "10.20.30.1", False)
    d = set_network(d, dhcp=True, port=board.port, **LOOP)
    assert d.dhcp and d.ip == "10.20.30.40"


def test_refusals_and_no_answer(board):
    with pytest.raises(NetworkSetupError, match="no RDS3 with MAC 7a:c5:ba:d8:51:d7"):
        set_network("7a:c5:ba:d8:51:d7", ip="10.0.0.5", subnet="255.255.255.0", timeout=0.5, port=board.port, **LOOP)
    board.refuse = "subnet must be /8 to /30"
    with pytest.raises(NetworkSetupError, match="refused the setting: subnet"):
        set_network(board.mac.hex(), ip="10.0.0.5", subnet=24, port=board.port, **LOOP)
    assert "set_network_request" in board.requests


@pytest.mark.parametrize("kwargs, message", [
    (dict(ip="10.0.0.5"), "static setting needs ip and subnet"),
    (dict(ip="10.0.0.5", subnet="255.0.255.0"), "invalid subnet"),
    (dict(ip="10.0.0.5", subnet=31), "/8 to /30"),
    (dict(ip="10.0.0.0", subnet=24), "network or broadcast"),
    (dict(ip="10.0.0.5", subnet=24, gateway="10.0.1.1"), "not another host"),
    (dict(dhcp=True, subnet=24), "give ip"),
])
def test_bad_settings_are_caught_before_sending(kwargs, message):
    with pytest.raises(ValueError, match=message):
        set_network("7a:c5:ba:d8:51:d6", **kwargs, **LOOP)


def test_command_line(board, capsys, monkeypatch):
    import rds3.__main__ as cli
    from rds3.__main__ import main
    monkeypatch.setattr(cli, "discover", lambda timeout, interface, all_boards: discover(0.5, port=board.port, **LOOP))
    assert main(["discover"]) == 0 and "7a:c5:ba:d8:51:d6" in capsys.readouterr().out
    monkeypatch.setattr(cli, "set_network", lambda *a: set_network(*a[:5], port=board.port, **LOOP))
    assert main(["set-network", "7a:c5:ba:d8:51:d6", "--ip", "10.1.1.9", "--subnet", "24"]) == 0
    assert "10.1.1.9/24" in capsys.readouterr().out
    assert main(["set-network", "7a:c5:ba:d8:51:d6", "--ip", "10.1.1.9"]) == 1
    assert "needs ip and subnet" in capsys.readouterr().err
