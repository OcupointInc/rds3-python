import pytest

from rds3 import Calibration, Mode, RDS3, RDS3Error
from fake_rds3 import FakeRDS3

CAL = Calibration.from_dict({"unit": "TEST", "frequency_mhz": [400, 500, 650],
                             "level_at_0db_dbm": [-20.0, -22.0, -27.0]})


@pytest.fixture
def board():
    fake = FakeRDS3()
    yield fake
    fake.close()


def connect(fake, **kw):
    return RDS3("127.0.0.1", fake.port, timeout=2, calibration=kw.pop("calibration", CAL), **kw)


def test_refuses_unsupported_firmware_and_the_wrong_lo_plan():
    for kwargs, message in (({"firmware": "3.0.0-rc2"}, "not supported"), ({"divider": 4}, "11.8 GHz")):
        fake = FakeRDS3(**kwargs)
        with pytest.raises(RDS3Error, match=message):
            connect(fake)
        fake.close()


def test_through_and_external_cal_keep_the_barracuda_off(board):
    r = connect(board)
    r.set_frontends(True, attenuation_db=12)
    r.set_mode("external_cal")
    assert (board.banks, board.external, board.rf, board.att) == (3, True, False, 12)
    assert r.status().mode is Mode.EXTERNAL_CAL
    r.set_mode(Mode.INTERNAL_CAL); r.set_mode(Mode.EXTERNAL_CAL)        # from the internal source too
    assert (board.banks, board.external, board.rf) == (3, True, False)
    r.set_mode("through")
    assert (board.banks, board.rf) == (0, False) and r.status().mode is Mode.THROUGH
    with pytest.raises(ValueError, match="mode must be one of"):
        r.set_mode("loopback")


def test_internal_cal_cw_sets_the_11_8_ghz_lo_and_the_calibrated_attenuation(board):
    r = connect(board)
    r.set_frontends(True, attenuation_db=0)
    r.set_mode(Mode.INTERNAL_CAL)
    tone = r.cw(500, level_dbm=-50)
    assert (board.lo, board.pll, board.banks, board.external, board.rf) == (11800, 11300, 3, False, True)
    assert board.dsa_q == 112 and tone["attenuator_db"] == 28.0 and tone["level_dbm"] == -50.0   # -22 - 28
    assert r.status().mode is Mode.INTERNAL_CAL and r.status().cal_source_running


def test_sweep_never_exceeds_the_level_and_ramps_the_synthesizer_upward(board):
    r = connect(board)
    r.set_frontends(True)
    r.set_mode("internal_cal")
    tone = r.sweep(400, 650, 0.5, level_dbm=-45)
    assert board.chirp == (11150, 250, 500000, 0, True)          # RF 11150 -> 11400 = IF 650 -> 400
    assert tone["attenuator_db"] == 25.0 and tone["level_dbm"] == -45.0 and tone["level_min_dbm"] == -52.0


def test_level_limits(board):
    r = connect(board)
    with pytest.raises(ValueError, match="-38"):
        r.set_level(-37.5)
    r.set_frontends(True); r.set_mode("internal_cal"); r.set_level(-70)
    with pytest.raises(RDS3Error, match="below the lowest level"):
        r.cw(650)                                                   # -27 - 31.75 = -58.75 dBm floor
    r.set_level(-50)
    with pytest.raises(ValueError, match="calibrated range"):
        r.cw(700)
    weak = connect(board, calibration=Calibration.from_dict({"frequency_mhz": [400, 650], "level_at_0db_dbm": [-41, -41]}))
    weak.set_level(-38)
    with pytest.raises(RDS3Error, match="above what the source reaches"):
        weak.cw(500)


def test_settings_made_before_internal_cal_apply_when_it_starts(board):
    r = connect(board)
    r.set_frontends(True)
    stored = r.cw(400)
    assert stored["applied"] is False and "set_pll_frequency_request" not in board.log
    r.set_level(-40)
    r.set_mode("internal_cal")
    assert board.pll == 11400 and board.dsa_q == 80 and board.banks == 3   # -20 - 20 = -40 dBm


def test_internal_cal_fault_turns_the_source_off_and_says_why():
    fake = FakeRDS3(lock=False)
    r = connect(fake)
    r.set_frontends(True)
    with pytest.raises(RDS3Error, match="synthesizer lock"):
        r.set_mode("internal_cal")
    assert fake.rf is False and fake.banks == 0
    fake.close()


def test_frontends_off_returns_to_through(board):
    r = connect(board)
    r.set_frontends(True); r.set_mode("internal_cal")
    r.off()
    assert (board.frontend, board.rf, board.banks) == (False, False, 0) and r.status().mode == "through"
    with pytest.raises(RDS3Error, match="frontends on"):
        r.set_mode("external_cal")
