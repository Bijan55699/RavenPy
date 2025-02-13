import datetime as dt
import os
import tempfile
import zipfile
from dataclasses import astuple, replace
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from ravenpy.config.commands import (
    ChannelProfileCommand,
    EvaluationPeriod,
    GriddedForcingCommand,
    GridWeightsCommand,
    HRUStateVariableTableCommand,
    LandUseClassesCommand,
    ObservationDataCommand,
    SBGroupPropertyMultiplierCommand,
    SoilClassesCommand,
    SoilProfilesCommand,
    Sub,
    VegetationClassesCommand,
)
from ravenpy.config.rvs import RVI
from ravenpy.models import (
    GR4JCN,
    GR4JCN_OST,
    HBVEC,
    HBVEC_OST,
    HMETS,
    HMETS_OST,
    MOHYSE,
    MOHYSE_OST,
    Raven,
    RavenError,
    get_average_annual_runoff,
)
from ravenpy.utilities.testdata import get_local_testdata

from .common import _convert_2d

TS = get_local_testdata(
    "raven-gr4j-cemaneige/Salmon-River-Near-Prince-George_meteo_daily.nc"
)

# Link to THREDDS Data Server netCDF testdata
TDS = "https://pavics.ouranos.ca/twitcher/ows/proxy/thredds/dodsC/birdhouse/testdata/raven"


@pytest.fixture
def input2d(tmpdir):
    """Convert 1D input to 2D output by copying all the time series along a new region dimension."""
    ds = _convert_2d(TS)
    fn_out = os.path.join(tmpdir, "input2d.nc")
    ds.to_netcdf(fn_out)
    return Path(fn_out)


def test_race():
    model1 = GR4JCN()
    model1.config.rvi.suppress_output = True
    model2 = GR4JCN()
    ost = GR4JCN_OST()

    assert model1.config.rvi.suppress_output.startswith(":SuppressOutput")
    assert model2.config.rvi.suppress_output == ""
    assert ost.config.rvi.suppress_output.startswith(":SuppressOutput")


# Salmon catchment is now split into land- and lake-part.
# The areas do not sum up to overall area of 4250.6 [km2].
# This is the reason the "test_routing" will give different
# results compared to "test_simple". The "salmon_land_hru"
# however is kept at the overall area of 4250.6 [km2] such
# that other tests still obtain same results as before.
salmon_land_hru_1 = dict(
    area=4250.6, elevation=843.0, latitude=54.4848, longitude=-123.3659
)
salmon_lake_hru_1 = dict(area=100.0, elevation=839.0, latitude=54.0, longitude=-123.4)
salmon_land_hru_2 = dict(
    area=2000.0, elevation=835.0, latitude=54.123, longitude=-123.4234
)


class TestGR4JCN:
    def test_error(self):
        model = GR4JCN()

        model.config.rvp.params = model.Params(
            0.529, -3.396, 407.29, 1.072, 16.9, 0.947
        )

        with pytest.raises(RavenError) as exc:
            model(TS)

        assert "CHydroUnit constructor:: HRU 1 has a negative or zero area" in str(
            exc.value
        )

    def test_simple(self):
        model = GR4JCN()

        model.config.rvi.start_date = dt.datetime(2000, 1, 1)
        model.config.rvi.end_date = dt.datetime(2002, 1, 1)
        model.config.rvi.run_name = "test"

        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        model.config.rvp.params = model.Params(
            0.529, -3.396, 407.29, 1.072, 16.9, 0.947
        )

        total_area_in_m2 = model.config.rvh.hrus[0].area * 1000 * 1000
        model.config.rvp.avg_annual_runoff = get_average_annual_runoff(
            TS, total_area_in_m2
        )

        np.testing.assert_almost_equal(
            model.config.rvp.avg_annual_runoff, 208.4805694844741
        )

        assert model.config.rvi.suppress_output == ""

        model(TS)

        # ------------
        # Check quality (diagnostic) of simulated streamflow values
        # ------------
        d = model.diagnostics
        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -0.117301, 4)

        # ------------
        # Check simulated streamflow values q_sim
        # ------------
        hds = model.q_sim

        assert hds.attrs["long_name"] == "Simulated outflows"

        assert len(hds.nbasins) == 1  # number of "gauged" basins is 1

        # We only have one SB with gauged=True, so the output has a single column.
        # The number of time steps simulated between (2000, 1, 1) and
        # (2002, 1, 1) is 732.
        assert hds.shape == (732, 1)

        # Check simulated streamflow at first three timesteps and three simulated
        # timesteps in the middle of the simulation period.
        dates = (
            "2000-01-01",
            "2000-01-02",
            "2000-01-03",
            "2001-01-30",
            "2001-01-31",
            "2001-02-01",
        )

        target_q_sim = [0.0, 0.165788, 0.559366, 12.374606, 12.33398, 12.293458]

        for t in range(6):
            np.testing.assert_almost_equal(
                hds.sel(nbasins=0, time=dates[t]), target_q_sim[t], 4
            )

        # ------------
        # Check parser
        # ------------

        assert model.config.rvi.calendar == RVI.CalendarOptions.GREGORIAN.value

        # ------------
        # Check saved HRU states saved in RVC
        # ------------
        assert 1 in model.solution.hru_states

        # ------------
        # Check attributes
        # ------------
        assert model.hydrograph.attrs["model_id"] == "gr4jcn"

    def test_routing(self):
        """We need at least 2 subbasins to activate routing."""
        model = GR4JCN()

        ts_2d = get_local_testdata(
            "raven-gr4j-cemaneige/Salmon-River-Near-Prince-George_meteo_daily_2d.nc"
        )

        #########
        # R V I #
        #########

        model.config.rvi.start_date = dt.datetime(2000, 1, 1)
        model.config.rvi.end_date = dt.datetime(2002, 1, 1)
        model.config.rvi.run_name = "test_gr4jcn_routing"
        model.config.rvi.routing = "ROUTE_DIFFUSIVE_WAVE"

        #########
        # R V H #
        #########

        # Here we assume that we have two subbasins. The first one (subbasin_id=10)
        # has a lake (hru_id=2; area-100km2) and the rest is covered by land (hru_id=1;
        # area=4250.6km2). The second subbasin (subbasin_id=20) does not contain a
        # lake and is hence only land (hru_id=3; area=2000km2).
        #
        # Later the routing product will tell us which basin flows into which. Here
        # we assume that the first subbasin (subbasin_id=10) drains into the second
        # (subbasin_id=20). At the outlet of this second one we have an observation
        # station (see :ObservationData in RVT). We will compare these observations
        # with the simulated streamflow. That is the reason why "gauged=True" for
        # the second basin.

        # HRU IDs are 1 to 3
        model.config.rvh.hrus = (
            GR4JCN.LandHRU(hru_id=1, subbasin_id=10, **salmon_land_hru_1),
            GR4JCN.LakeHRU(hru_id=2, subbasin_id=10, **salmon_lake_hru_1),
            GR4JCN.LandHRU(hru_id=3, subbasin_id=20, **salmon_land_hru_2),
        )

        # Sub-basin IDs are 10 and 20 (not 1 and 2), to help disambiguate
        model.config.rvh.subbasins = (
            # gauged = False:
            # Usually this output would only be written for user's convenience.
            # There is usually no observation of streamflow available within
            # catchments; only at the outlet. That's most commonly the reason
            # why a catchment is defined as it is defined.
            Sub(
                name="upstream",
                subbasin_id=10,
                downstream_id=20,
                profile="chn_10",
                gauged=False,
            ),
            # gauged = True:
            # Since this is the outlet, this would usually be what we calibrate
            # against (i.e. we try to match this to Qobs).
            Sub(
                name="downstream",
                subbasin_id=20,
                downstream_id=-1,
                profile="chn_20",
                gauged=True,
            ),
        )

        model.config.rvh.land_subbasin_property_multiplier = (
            SBGroupPropertyMultiplierCommand("Land", "MANNINGS_N", 1.0)
        )
        model.config.rvh.lake_subbasin_property_multiplier = (
            SBGroupPropertyMultiplierCommand("Lakes", "RESERVOIR_CREST_WIDTH", 1.0)
        )

        #########
        # R V T #
        #########

        gws = GridWeightsCommand(
            number_hrus=3,
            number_grid_cells=1,
            # Here we have a special case: station is 0 for every row because the example NC
            # has only one region/station (which is column 0)
            data=((1, 0, 1.0), (2, 0, 1.0), (3, 0, 1.0)),
        )
        # These will be shared (inline) to all the StationForcing commands in the RVT
        model.config.rvt.grid_weights = gws

        #########
        # R V P #
        #########

        model.config.rvp.params = model.Params(
            0.529, -3.396, 407.29, 1.072, 16.9, 0.947
        )

        total_area_in_km2 = sum(hru.area for hru in model.config.rvh.hrus)
        total_area_in_m2 = total_area_in_km2 * 1000 * 1000
        model.config.rvp.avg_annual_runoff = get_average_annual_runoff(
            ts_2d, total_area_in_m2
        )

        np.testing.assert_almost_equal(
            model.config.rvp.avg_annual_runoff, 139.5407534171111
        )

        # These channel profiles describe the geometry of the actual river crossection.
        # The eight points (x) to describe the following geometry are given in each
        # profile:
        #
        # ----x                                     x---
        #      \           FLOODPLAIN             /
        #       x----x                     x----x
        #             \                  /
        #               \   RIVERBED   /
        #                 x----------x
        #
        model.config.rvp.channel_profiles = [
            ChannelProfileCommand(
                name="chn_10",
                bed_slope=7.62066e-05,
                survey_points=[
                    (0, 463.647),
                    (16.0, 459.647),
                    (90.9828, 459.647),
                    (92.9828, 458.647),
                    (126.4742, 458.647),
                    (128.4742, 459.647),
                    (203.457, 459.647),
                    (219.457, 463.647),
                ],
                roughness_zones=[
                    (0, 0.0909167),
                    (90.9828, 0.035),
                    (128.4742, 0.0909167),
                ],
            ),
            ChannelProfileCommand(
                name="chn_20",
                bed_slope=9.95895e-05,
                survey_points=[
                    (0, 450.657),
                    (16.0, 446.657),
                    (85.0166, 446.657),
                    (87.0166, 445.657),
                    (117.5249, 445.657),
                    (119.5249, 446.657),
                    (188.54149999999998, 446.657),
                    (204.54149999999998, 450.657),
                ],
                roughness_zones=[
                    (0, 0.0915769),
                    (85.0166, 0.035),
                    (119.5249, 0.0915769),
                ],
            ),
        ]

        #############
        # Run model #
        #############

        model(ts_2d)

        ###########
        # Verify  #
        ###########

        hds = model.q_sim

        assert len(hds.nbasins) == 1  # number of "gauged" basins is 1

        # We only have one SB with gauged=True, so the output has a single column.
        # The number of time steps simulated between (2000, 1, 1) and
        # (2002, 1, 1) is 732.
        assert hds.shape == (732, 1)

        # Check simulated streamflow at first three timesteps and three simulated
        # timesteps in the middle of the simulation period.
        dates = (
            "2000-01-01",
            "2000-01-02",
            "2000-01-03",
            "2001-01-30",
            "2001-01-31",
            "2001-02-01",
        )

        target_q_sim = [0.0, 0.304073, 0.980807, 17.54049, 17.409493, 17.437954]

        for t in range(6):
            np.testing.assert_almost_equal(
                hds.sel(nbasins=0, time=dates[t]), target_q_sim[t], 4
            )

        # For lumped GR4J model we have 1 subbasin and 1 HRU as well as no routing, no
        # channel profiles, and the area of the entire basin is 4250.6 [km2]. Comparison
        # of simulated and observed streamflow at outlet yielded:
        # np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -0.116971, 4)
        #
        # This is now a different value due to:
        # - basin we have here is larger (4250.6 [km2] + 100 [km2] + 2000.0 [km2])
        # - we do routing: so water from subbasin 1 needs some time to arrive at the
        #   outlet of subbasin 2
        d = model.diagnostics
        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -0.0141168, 4)

    def test_config_update(self):
        model = GR4JCN()

        # This is a regular attribute member
        model.config.update("run_name", "test")
        assert model.config.rvi.run_name == "test"

        # This is a computed property
        model.config.update("evaporation", "PET_FROMMONTHLY")
        assert model.config.rvi.evaporation == "PET_FROMMONTHLY"

        # Existing property but wrong value (the enum cast should throw an error)
        with pytest.raises(ValueError):
            model.config.update("routing", "WRONG")

        # Non-existing attribute
        with pytest.raises(AttributeError):
            model.config.update("why", "not?")

        # Params

        model.config.update(
            "params", np.array([0.529, -3.396, 407.29, 1.072, 16.9, 0.947])
        )
        assert model.config.rvp.params.GR4J_X1 == 0.529

        model.config.update("params", [0.529, -3.396, 407.29, 1.072, 16.9, 0.947])
        assert model.config.rvp.params.GR4J_X1 == 0.529

        model.config.update("params", (0.529, -3.396, 407.29, 1.072, 16.9, 0.947))
        assert model.config.rvp.params.GR4J_X1 == 0.529

    def test_run(self):
        model = GR4JCN()

        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        model(
            TS,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            params=(0.529, -3.396, 407.29, 1.072, 16.9, 0.947),
            suppress_output=False,
        )
        d = model.diagnostics

        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -0.117301, 4)

    def test_evaluation(self):
        model = GR4JCN()

        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        model(
            TS,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            params=(0.529, -3.396, 407.29, 1.072, 16.9, 0.947),
            suppress_output=False,
            evaluation_metrics=["RMSE", "KLING_GUPTA"],
            evaluation_periods=[
                EvaluationPeriod("period1", "2000-01-01", "2000-12-31"),
                EvaluationPeriod("period2", "2001-01-01", "2001-12-31"),
            ],
        )
        d = model.diagnostics
        assert "DIAG_RMSE" in d
        assert "DIAG_KLING_GUPTA" in d
        assert len(d["DIAG_RMSE"]) == 3  # ALL, period1, period2

    def test_run_new_hrus_param(self):
        model = GR4JCN()

        model(
            TS,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            params=(0.529, -3.396, 407.29, 1.072, 16.9, 0.947),
            suppress_output=False,
            hrus=(GR4JCN.LandHRU(**salmon_land_hru_1),),
        )
        d = model.diagnostics

        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -0.117301, 4)

    # @pytest.mark.skip
    def test_overwrite(self):
        model = GR4JCN()

        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        model(
            TS,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            params=(0.529, -3.396, 407.29, 1.072, 16.9, 0.947),
        )
        assert model.config.rvi.suppress_output == ""

        qsim1 = model.q_sim.copy(deep=True)
        m1 = qsim1.mean()

        # This is only needed temporarily while we fix this: https://github.com/CSHS-CWRA/RavenPy/issues/4
        # Please remove when fixed!
        model.hydrograph.close()  # Needed with xarray 0.16.1

        model(TS, params=(0.5289, -3.397, 407.3, 1.071, 16.89, 0.948), overwrite=True)

        qsim2 = model.q_sim.copy(deep=True)
        m2 = qsim2.mean()

        # This is only needed temporarily while we fix this: https://github.com/CSHS-CWRA/RavenPy/issues/4
        # Please remove when fixed!
        model.hydrograph.close()  # Needed with xarray 0.16.1

        assert m1 != m2

        np.testing.assert_almost_equal(m1, m2, 1)

        d = model.diagnostics

        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -0.117315, 4)

        model.config.rvc.hru_states[1] = HRUStateVariableTableCommand.Record(soil0=0)

        # Set initial conditions explicitly
        model(
            TS,
            end_date=dt.datetime(2001, 2, 1),
            # hru_state=HRUStateVariableTableCommand.Record(soil0=0),
            overwrite=True,
        )
        assert model.q_sim.isel(time=1).values[0] < qsim2.isel(time=1).values[0]

    def test_resume(self):
        model_ab = GR4JCN()
        model_ab.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)
        kwargs = dict(
            params=(0.529, -3.396, 407.29, 1.072, 16.9, 0.947),
        )
        # Reference run
        model_ab(
            TS,
            run_name="run_ab",
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2001, 1, 1),
            **kwargs,
        )

        model_a = GR4JCN()

        model_a.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)
        model_a(
            TS,
            run_name="run_a",
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2000, 7, 1),
            **kwargs,
        )

        # Path to solution file from run A
        rvc = model_a.outputs["solution"]

        # Resume with final state from live model
        model_a.resume()

        model_a(
            TS,
            run_name="run_2",
            start_date=dt.datetime(2000, 7, 1),
            end_date=dt.datetime(2001, 1, 1),
            **kwargs,
        )

        for key in ["Soil Water[0]", "Soil Water[1]"]:
            np.testing.assert_array_almost_equal(
                model_a.storage[key] - model_ab.storage[key], 0, 5
            )

        # Resume with final state from saved solution file
        model_b = GR4JCN()
        model_b.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)
        model_b.resume(
            rvc
        )  # <--------- And this is how you feed it to a brand new model.
        model_b(
            TS,
            run_name="run_2",
            start_date=dt.datetime(2000, 7, 1),
            end_date=dt.datetime(2001, 1, 1),
            **kwargs,
        )

        for key in ["Soil Water[0]", "Soil Water[1]"]:
            np.testing.assert_array_almost_equal(
                model_b.storage[key] - model_ab.storage[key], 0, 5
            )

        # model.solution loads the solution in a dictionary. I expected the variables to be identical,
        # but some atmosphere related attributes are way off. Is it possible that `ATMOSPHERE` and `ATMOS_PRECIP` are
        # cumulative sums of precipitation over the run ?
        # assert model_b.solution == model_ab.solution # This does not work. Atmosphere attributes are off.

    def test_resume_earlier(self):
        """Check that we can resume a run with the start date set at another date than the time stamp in the
        solution."""
        params = (0.529, -3.396, 407.29, 1.072, 16.9, 0.947)
        # Reference run
        model = GR4JCN()
        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)
        model(
            TS,
            run_name="run_a",
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2000, 2, 1),
            params=params,
        )

        s_a = model.storage["Soil Water[0]"].isel(time=-1)

        # Path to solution file from run A
        rvc = model.outputs["solution"]

        # Resume with final state from live model
        # We have two options to do this:
        # 1. Replace model template by solution file as is: model.resume()
        # 2. Replace variable in RVC class by parsed values: model.rvc.parse(rvc.read_text())
        # I think in many cases option 2 will prove simpler.

        model.config.rvc.parse_solution(rvc.read_text())

        model(
            TS,
            run_name="run_b",
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2000, 2, 1),
            params=params,
        )

        s_b = model.storage["Soil Water[0]"].isel(time=-1)
        assert s_a != s_b

    def test_update_soil_water(self):
        params = (0.529, -3.396, 407.29, 1.072, 16.9, 0.947)
        # Reference run
        model = GR4JCN()
        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)
        model(
            TS,
            run_name="run_a",
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2000, 2, 1),
            params=params,
        )

        s_0 = float(model.storage["Soil Water[0]"].isel(time=-1).values)
        s_1 = float(model.storage["Soil Water[1]"].isel(time=-1).values)

        # hru_state = replace(model.rvc.hru_state, soil0=s_0, soil1=s_1)
        model.config.rvc.hru_states[1] = replace(
            model.config.rvc.hru_states[1], soil0=s_0, soil1=s_1
        )

        model(
            TS,
            run_name="run_b",
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2000, 2, 1),
            # hru_state=hru_state,
            params=params,
        )

        assert s_0 != model.storage["Soil Water[0]"].isel(time=-1)
        assert s_1 != model.storage["Soil Water[1]"].isel(time=-1)

    def test_version(self):
        model = Raven()
        assert model.raven_version == "3.0.4"

        model = GR4JCN()
        assert model.raven_version == "3.0.4"

    def test_parallel_params(self):
        model = GR4JCN()
        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        model(
            TS,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            params=[
                (0.529, -3.396, 407.29, 1.072, 16.9, 0.947),
                (0.528, -3.4, 407.3, 1.07, 17, 0.95),
            ],
            suppress_output=False,
        )

        assert len(model.diagnostics) == 2
        assert model.hydrograph.dims["params"] == 2
        z = zipfile.ZipFile(model.outputs["rv_config"])
        assert len(z.filelist) == 10

    def test_parallel_basins(self, input2d):
        ts = input2d
        model = GR4JCN()
        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        model(
            ts,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            params=[0.529, -3.396, 407.29, 1.072, 16.9, 0.947],
            nc_index=[0, 0],
            # name=["basin1", "basin2"],  # Not sure about this..
            suppress_output=False,
        )

        assert len(model.diagnostics) == 2
        assert len(model.hydrograph.nbasins) == 2
        np.testing.assert_array_equal(
            model.hydrograph.basin_name[:], ["sub_001", "sub_001"]
        )
        z = zipfile.ZipFile(model.outputs["rv_config"])
        assert len(z.filelist) == 10

    @pytest.mark.online
    def test_dap(self):
        """Test Raven with DAP link instead of local netCDF file."""
        model = GR4JCN()
        config = dict(
            start_date=dt.datetime(2000, 6, 1),
            end_date=dt.datetime(2000, 6, 10),
            run_name="test",
            hrus=(GR4JCN.LandHRU(**salmon_land_hru_1),),
            params=model.Params(0.529, -3.396, 407.29, 1.072, 16.9, 0.947),
        )

        ts = (
            f"{TDS}/raven-gr4j-cemaneige/Salmon-River-Near-Prince-George_meteo_daily.nc"
        )
        model(ts, **config)

    @pytest.mark.online
    def test_canopex(self):
        CANOPEX_DAP = (
            "https://pavics.ouranos.ca/twitcher/ows/proxy/thredds/dodsC/birdhouse/ets"
            "/Watersheds_5797_cfcompliant.nc"
        )
        model = GR4JCN()
        config = dict(
            start_date=dt.datetime(2010, 6, 1),
            end_date=dt.datetime(2010, 6, 10),
            nc_index=5600,
            run_name="Test_run",
            rain_snow_fraction="RAINSNOW_DINGMAN",
            tasmax={"offset": -273.15},
            tasmin={"offset": -273.15},
            pr={"scale": 86400.0},
            hrus=[
                model.LandHRU(
                    area=3650.47, latitude=49.51, longitude=-95.72, elevation=330.59
                )
            ],
            params=model.Params(108.02, 2.8693, 25.352, 1.3696, 1.2483, 0.30679),
        )
        model(ts=CANOPEX_DAP, **config)


class TestGR4JCN_OST:
    def test_simple(self):
        model = GR4JCN_OST()
        model.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        # Parameter bounds
        low = (0.01, -15.0, 10.0, 0.0, 1.0, 0.0)
        high = (2.5, 10.0, 700.0, 7.0, 30.0, 1.0)

        model.configure(
            get_local_testdata("ostrich-gr4j-cemaneige/OstRandomNumbers.txt")
        )

        model(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            lowerBounds=low,
            upperBounds=high,
            algorithm="DDS",
            random_seed=0,
            max_iterations=10,
        )

        d = model.diagnostics

        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], 0.50717, 4)

        # Random number seed: 123
        # Budget:             10
        # Algorithm:          DDS
        # :StartDate          1954-01-01 00:00:00
        # :Duration           208
        opt_para = astuple(model.calibrated_params)
        opt_func = model.obj_func

        np.testing.assert_almost_equal(
            opt_para,
            [2.424726, 3.758972, 204.3856, 5.866946, 16.60408, 0.3728098],
            4,
            err_msg="calibrated parameter set is not matching expected value",
        )

        np.testing.assert_almost_equal(
            opt_func,
            -0.50717,
            4,
            err_msg="calibrated NSE is not matching expected value",
        )

        # # Random number seed: 123
        # # Budget:             50
        # # Algorithm:          DDS
        # # :StartDate          1954-01-01 00:00:00
        # # :Duration           20819
        # np.testing.assert_almost_equal( opt_para, [0.3243268,3.034247,407.2890,2.722774,12.18124,0.9468769], 4,
        #                                 err_msg='calibrated parameter set is not matching expected value')
        # np.testing.assert_almost_equal( opt_func, -0.5779910, 4,
        #                                 err_msg='calibrated NSE is not matching expected value')
        gr4j = GR4JCN()
        gr4j.config.rvh.hrus = (GR4JCN.LandHRU(**salmon_land_hru_1),)

        gr4j(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            params=model.calibrated_params,
        )

        np.testing.assert_almost_equal(
            gr4j.diagnostics["DIAG_NASH_SUTCLIFFE"], d["DIAG_NASH_SUTCLIFFE"]
        )


class TestHMETS:
    def test_simple(self):
        model = HMETS()
        params = (
            9.5019,
            0.2774,
            6.3942,
            0.6884,
            1.2875,
            5.4134,
            2.3641,
            0.0973,
            0.0464,
            0.1998,
            0.0222,
            -1.0919,
            2.6851,
            0.3740,
            1.0000,
            0.4739,
            0.0114,
            0.0243,
            0.0069,
            310.7211,
            916.1947,
        )

        model(
            TS,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            params=params,
            suppress_output=True,
        )

        d = model.diagnostics

        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -3.0132, 4)


class TestHMETS_OST:
    def test_simple(self):
        model = HMETS_OST()

        model.configure(
            get_local_testdata("ostrich-gr4j-cemaneige/OstRandomNumbers.txt")
        )

        # Parameter bounds
        low = (
            0.3,
            0.01,
            0.5,
            0.15,
            0.0,
            0.0,
            -2.0,
            0.01,
            0.0,
            0.01,
            0.005,
            -5.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.00001,
            0.0,
            0.00001,
            0.0,
            0.0,
        )
        high = (
            20.0,
            5.0,
            13.0,
            1.5,
            20.0,
            20.0,
            3.0,
            0.2,
            0.1,
            0.3,
            0.1,
            2.0,
            5.0,
            1.0,
            3.0,
            1.0,
            0.02,
            0.1,
            0.01,
            0.5,
            2.0,
        )

        model(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            lowerBounds=low,
            upperBounds=high,
            algorithm="DDS",
            random_seed=0,
            max_iterations=10,
        )

        d = model.diagnostics

        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -1.43474, 4)

        opt_para = model.optimized_parameters
        opt_func = model.obj_func

        # # Random number seed: 123
        # # Budget:             50
        # # Algorithm:          DDS
        # # :StartDate          1954-01-01 00:00:00
        # # :Duration           20819
        # np.testing.assert_almost_equal( opt_para, [0.3243268,3.034247,407.2890,2.722774,12.18124,0.9468769], 4,
        #                                 err_msg='calibrated parameter set is not matching expected value')
        # np.testing.assert_almost_equal( opt_func, -0.5779910, 4,
        #                                 err_msg='calibrated NSE is not matching expected value')
        #
        # Random number seed: 123                         #
        # Budget:             10                          #      This is the setup used for testing:
        # Algorithm:          DDS                         #         shorter sim-period and lower budget
        # :StartDate          1954-01-01 00:00:00         #      First tested that example below matches
        # :Duration           208                         #

        expected_value = [
            1.777842e01,
            3.317211e00,
            5.727342e00,
            1.419491e00,
            1.382141e01,
            1.637954e01,
            7.166296e-01,
            1.389346e-01,
            2.620464e-02,
            2.245525e-01,
            2.839426e-02,
            -2.003810e00,
            9.479623e-01,
            4.803857e-01,
            2.524914e00,
            4.117232e-01,
            1.950058e-02,
            4.494123e-02,
            1.405815e-03,
            2.815803e-02,
            1.007823e00,
        ]
        np.testing.assert_almost_equal(
            opt_para,
            expected_value,
            4,
            err_msg="calibrated parameter set is not matching expected value",
        )
        np.testing.assert_almost_equal(
            opt_func,
            1.43474,
            4,
            err_msg="calibrated NSE is not matching expected value",
        )

        # # Random number seed: 123                       #
        # # Budget:             50                        #      This is the setup in the Wiki:
        # # Algorithm:          DDS                       #      https://github.com/Ouranosinc/raven/wiki/
        # # :StartDate          1954-01-01 00:00:00       #      Technical-Notes#example-setups-for-hmets
        # # :Duration           20819                     #
        # np.testing.assert_almost_equal(opt_para, [5.008045E+00, 7.960246E-02, 4.332698E+00, 4.978125E-01,
        #                                           1.997029E+00, 6.269773E-01, 1.516961E+00, 8.180383E-02,
        #                                           6.730663E-02, 2.137822E-02, 2.097163E-02, 1.773348E+00,
        #                                           3.036039E-01, 1.928524E-02, 1.758471E+00, 8.942299E-01,
        #                                           8.741980E-03, 5.036474E-02, 9.465804E-03, 1.851839E-01,
        #                                           1.653934E-01, 2.624006E+00, 8.868485E-02, 9.259195E+01,
        #                                           8.269670E+01], 4,
        #                                err_msg='calibrated parameter set is not matching expected value')
        # np.testing.assert_almost_equal(opt_func, -6.350490E-01, 4,
        #                                err_msg='calibrated NSE is not matching expected value')
        hmets = HMETS()
        hmets(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            params=model.calibrated_params,
        )

        np.testing.assert_almost_equal(
            hmets.diagnostics["DIAG_NASH_SUTCLIFFE"], d["DIAG_NASH_SUTCLIFFE"], 4
        )


class TestMOHYSE:
    def test_simple(self):
        model = MOHYSE()
        params = (
            1.0,
            0.0468,
            4.2952,
            2.658,
            0.4038,
            0.0621,
            0.0273,
            0.0453,
            0.9039,
            5.6167,
        )

        model(
            TS,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            params=params,
            suppress_output=True,
        )

        d = model.diagnostics
        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], 0.194612, 4)


class TestMOHYSE_OST:
    def test_simple(self):
        model = MOHYSE_OST()

        model.configure(
            get_local_testdata("ostrich-gr4j-cemaneige/OstRandomNumbers.txt")
        )

        # Parameter bounds
        low_p = (0.01, 0.01, 0.01, -5.00, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01)
        high_p = (20.0, 1.0, 20.0, 5.0, 0.5, 1.0, 1.0, 1.0, 15.0, 15.0)

        model(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            lowerBounds=low_p,
            upperBounds=high_p,
            algorithm="DDS",
            random_seed=0,
            max_iterations=10,
        )

        d = model.diagnostics
        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], 0.3826810, 4)

        opt_para = model.optimized_parameters
        opt_func = model.obj_func

        # # Random number seed: 123
        # # Budget:             50
        # # Algorithm:          DDS
        # # :StartDate          1954-01-01 00:00:00
        # # :Duration           20819
        # np.testing.assert_almost_equal( opt_para, [0.3243268,3.034247,407.2890,2.722774,12.18124,0.9468769], 4,
        #                                 err_msg='calibrated parameter set is not matching expected value')
        # np.testing.assert_almost_equal( opt_func, -0.5779910, 4,
        #                                 err_msg='calibrated NSE is not matching expected value')
        #
        # Random number seed: 123                         #
        # Budget:             10                          #      This is the setup used for testing:
        # Algorithm:          DDS                         #         shorter sim-period and lower budget
        # :StartDate          1954-01-01 00:00:00         #      First tested that example below matches
        # :Duration           208                         #
        np.testing.assert_almost_equal(
            opt_para,
            [
                7.721801e00,
                8.551484e-01,
                1.774571e01,
                1.627677e00,
                7.702450e-02,
                9.409600e-01,
                6.941596e-01,
                8.207870e-01,
                8.154455e00,
                1.018226e01,
            ],
            4,
            err_msg="calibrated parameter set is not matching expected value",
        )
        np.testing.assert_almost_equal(
            opt_func,
            -0.3826810,
            4,
            err_msg="calibrated NSE is not matching expected value",
        )

        # # Random number seed: 123                       #
        # # Budget:             50                        #      This is the setup in the Wiki:
        # # Algorithm:          DDS                       #      https://github.com/Ouranosinc/raven/wiki/
        # # :StartDate          1954-01-01 00:00:00       #      Technical-Notes#example-setups-for-mohyse
        # # :Duration           20819                     #
        # np.testing.assert_almost_equal(opt_para, [1.517286E+01, 7.112556E-01, 1.981243E+01, -4.193046E+00,
        #                                           1.791486E-01, 9.774897E-01, 5.353541E-01, 6.686806E-01,
        #                                           1.040908E+01, 1.132304E+01, 8.831552E-02], 4,
        #                                err_msg='calibrated parameter set is not matching expected value')
        # np.testing.assert_almost_equal(opt_func, -0.3857010, 4,
        #                                err_msg='calibrated NSE is not matching expected value')
        mohyse = MOHYSE()
        mohyse(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            params=model.calibrated_params,
        )

        np.testing.assert_almost_equal(
            mohyse.diagnostics["DIAG_NASH_SUTCLIFFE"], d["DIAG_NASH_SUTCLIFFE"], 4
        )


class TestHBVEC:
    def test_simple(self):
        model = HBVEC()
        params = (
            0.05984519,
            4.072232,
            2.001574,
            0.03473693,
            0.09985144,
            0.506052,
            3.438486,
            38.32455,
            0.4606565,
            0.06303738,
            2.277781,
            4.873686,
            0.5718813,
            0.04505643,
            0.877607,
            18.94145,
            2.036937,
            0.4452843,
            0.6771759,
            1.141608,
            1.024278,
        )

        model(
            TS,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            params=params,
            suppress_output=True,
        )

        d = model.diagnostics
        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], 0.0186633, 4)

    def test_evap(self):
        model = HBVEC()
        params = (
            0.05984519,
            4.072232,
            2.001574,
            0.03473693,
            0.09985144,
            0.506052,
            3.438486,
            38.32455,
            0.4606565,
            0.06303738,
            2.277781,
            4.873686,
            0.5718813,
            0.04505643,
            0.877607,
            18.94145,
            2.036937,
            0.4452843,
            0.6771759,
            1.141608,
            1.024278,
        )

        model(
            TS,
            start_date=dt.datetime(2000, 1, 1),
            end_date=dt.datetime(2002, 1, 1),
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            params=params,
            suppress_output=True,
            evaporation="PET_OUDIN",
            ow_evaporation="PET_OUDIN",
        )


class TestHBVEC_OST:
    def test_simple(self):
        model = HBVEC_OST()

        model.configure(
            get_local_testdata("ostrich-gr4j-cemaneige/OstRandomNumbers.txt")
        )

        # Parameter bounds
        low = (
            -3.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.3,
            0.0,
            0.0,
            0.01,
            0.05,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.05,
            0.8,
            0.8,
        )
        high = (
            3.0,
            8.0,
            8.0,
            0.1,
            1.0,
            1.0,
            7.0,
            100.0,
            1.0,
            0.1,
            6.0,
            5.0,
            5.0,
            0.2,
            1.0,
            30.0,
            3.0,
            2.0,
            1.0,
            1.5,
            1.5,
        )

        model(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            lowerBounds=low,
            upperBounds=high,
            algorithm="DDS",
            random_seed=0,
            max_iterations=10,
        )

        d = model.diagnostics
        np.testing.assert_almost_equal(d["DIAG_NASH_SUTCLIFFE"], -2.25991e-01, 4)

        opt_para = astuple(model.calibrated_params)
        opt_func = model.obj_func

        # Random number seed: 123                         #
        # Budget:             10                          #      This is the setup used for testing:
        # Algorithm:          DDS                         #         shorter sim-period and lower budget
        # :StartDate          1954-01-01 00:00:00         #      First tested that example below matches
        # :Duration           208                         #
        np.testing.assert_almost_equal(
            opt_para,
            [
                -8.317931e-01,
                4.072232e00,
                2.001574e00,
                5.736299e-03,
                9.985144e-02,
                4.422529e-01,
                3.438486e00,
                8.055843e01,
                4.440133e-01,
                8.451082e-02,
                2.814201e00,
                7.327970e-01,
                1.119773e00,
                1.161223e-03,
                4.597179e-01,
                1.545857e01,
                1.223865e00,
                4.452843e-01,
                9.492006e-01,
                9.948123e-01,
                1.110682e00,
            ],
            4,
            err_msg="calibrated parameter set is not matching expected value",
        )
        np.testing.assert_almost_equal(
            opt_func,
            2.25991e-01,
            4,
            err_msg="calibrated NSE is not matching expected value",
        )

        # # Random number seed: 123                       #
        # # Budget:             50                        #      This is the setup in the Wiki:
        # # Algorithm:          DDS                       #      https://github.com/Ouranosinc/raven/wiki/
        # # :StartDate          1954-01-01 00:00:00       #      Technical-Notes#example-setups-for-environment-
        # # :Duration           20819                     #
        # np.testing.assert_almost_equal(opt_para, [5.984519E-02, 4.072232E+00, 2.001574E+00, 3.473693E-02,
        #                                           9.985144E-02, 5.060520E-01, 2.944343E+00, 3.832455E+01,
        #                                           4.606565E-01, 6.303738E-02, 2.277781E+00, 4.873686E+00,
        #                                           5.718813E-01, 4.505643E-02, 8.776511E-01, 1.894145E+01,
        #                                           2.036937E+00, 4.452843E-01, 6.771759E-01, 1.206053E+00,
        #                                           1.024278E+00], 4,
        #                                err_msg='calibrated parameter set is not matching expected value')
        # np.testing.assert_almost_equal(opt_func, -6.034670E-01, 4,
        #                                err_msg='calibrated NSE is not matching expected value')
        hbvec = HBVEC()
        hbvec(
            TS,
            start_date=dt.datetime(1954, 1, 1),
            duration=208,
            area=4250.6,
            elevation=843.0,
            latitude=54.4848,
            longitude=-123.3659,
            params=model.calibrated_params,
        )

        np.testing.assert_almost_equal(
            hbvec.diagnostics["DIAG_NASH_SUTCLIFFE"], d["DIAG_NASH_SUTCLIFFE"], 4
        )
