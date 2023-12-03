from datetime import datetime
import numpy as np
import pendulum
import streamlit as st

from api import (
    API,
    AuthenticationError,
    ElectricityMeterPoint,
    SavingSession,
)


@st.cache_data(ttl="1h")
def cache_sessions(_api: API):
    return [
        session
        for session in _api.saving_sessions()
        if session.startAt > pendulum.datetime(2023, 11, 1)
        and session.code.startswith("EVENT_")  # ignore test events
    ]


@st.cache_data(ttl=None)  # never expire
def get_product(code: str):
    api = API()  # unauthenticated
    return api.energy_product(code)


def weekday(day):
    """True if day is a weekday"""
    return pendulum.MONDAY <= day.day_of_week <= pendulum.FRIDAY


def phh(hh: int):
    return pendulum.duration(minutes=hh * 30)


class Readings:
    """Cached table of readings"""

    def __init__(self, meter_point: ElectricityMeterPoint):
        self.meter_point = meter_point
        self.requested = set()
        self.hh = {}

    def get_readings(self, api: API, ts: datetime, hh: int, debug):
        half_hours = list(pendulum.period(ts, ts + phh(hh - 1)).range("minutes", 30))
        if not self.requested.issuperset(half_hours):
            start_at = ts - phh(100 - hh)
            debug(f"Fetching {self.meter_point.mpan} readings from {start_at}")

            # Request readings and cache the lot
            readings = api.half_hourly_readings(
                mpan=self.meter_point.mpan,
                meter=self.meter_point.meters[0].id,
                start_at=start_at,
                first=100,
                before=None,
            )
            if readings:
                debug(
                    f"Received {len(readings)} readings from {readings[0].startAt} to {readings[-1].endAt}"
                )
                self.requested.update(
                    pendulum.period(start_at, readings[-1].startAt).range("minutes", 30)
                )
            else:
                debug("Received no readings")
                self.requested.update(
                    pendulum.period(start_at, start_at + phh(99)).range("minutes", 30)
                )

            for reading in readings:
                self.hh[reading.startAt] = reading.value

        try:
            values = [self.hh[t] for t in half_hours]
            return np.array(values)
        except KeyError:
            raise ValueError("missing readings")


def calculate(
    api: API,
    sessions: list[SavingSession],
    import_readings: Readings,
    export_readings: Readings | None,
    ss: SavingSession,
    tick,
    debug,
):
    # Baseline from meter readings from the same time as the Session over the past 10 weekdays (excluding any days with a Saving Session),
    # past 4 weekend days if Saving Session is on a weekend.
    days = 0
    baseline_days = 10 if weekday(ss.startAt) else 4
    baseline = np.zeros(ss.hh)
    previous_session_days = {ss.startAt.date() for ss in sessions}
    previous = pendulum.period(
        ss.startAt.subtract(days=1), ss.startAt.subtract(days=61)
    )

    try:
        ss_import = import_readings.get_readings(api, ss.startAt, ss.hh, debug)
        next(tick)
        if export_readings:
            ss_export = export_readings.get_readings(api, ss.startAt, ss.hh, debug)
        else:
            ss_export = np.zeros(ss.hh)  # no export
        next(tick)
        debug(f"session import: {ss_import}")
        debug(f"session export: {ss_export}")
    except ValueError:
        # incomplete, but useful to still calculate baseline
        debug("session incomplete")
        ss_import = ss_export = None

    for dt in previous.range("days"):
        if weekday(dt) != weekday(ss.startAt):
            continue
        if dt.date() in previous_session_days:
            continue
        try:
            import_values = import_readings.get_readings(api, dt, ss.hh, debug)
            baseline += import_values
            debug(f"baseline day #{days}: {dt} import: {import_values}")
            next(tick)

            if export_readings:
                export_values = export_readings.get_readings(api, dt, ss.hh, debug)
                baseline -= export_values
                debug(f"baseline day #{days}: {dt} export: {export_values}")
                next(tick)
            days += 1

            if days == baseline_days:
                break
        except ValueError:
            debug(f"skipped day: {dt} missing readings")

    baseline = baseline / days

    if ss_import is None or ss_export is None:
        # incomplete
        row = {
            "session": ss.startAt,
            "baseline": baseline.sum(),
        }
        return row

    # saving is calculated per settlement period (half hour), and only positive savings considered
    kwh = (baseline - ss_import + ss_export).clip(min=0)
    points = np.round(kwh * ss.rewardPerKwhInOctoPoints / 8) * 8
    reward = int(points.sum())

    row = {
        "session": ss.startAt,
        "import": ss_import.sum(),
        "export": ss_export.sum(),
        "baseline": baseline.sum(),
        "saved": kwh.sum(),
        "reward": reward,
        "earnings": reward / 800,
    }
    return row


def error(msg: str):
    st.error(msg, icon="⚠️")
    st.stop()


def debug_message(msg):
    st.write(msg)


def debug_noop(msg):
    pass


def main():
    debug = (
        debug_message if "debug" in st.experimental_get_query_params() else debug_noop
    )
    st.set_page_config(page_icon="🐙", page_title="Octopus Saving Sessions calculator")
    st.header("🐙 Octopus Saving Sessions calculator")

    st.subheader("Your Octopus API Key")
    st.markdown(
        "Find this in your online dashboard: https://octopus.energy/dashboard/developer/"
    )
    if "api_key" not in st.session_state and (
        api_key := st.experimental_get_query_params().get("api_key")
    ):
        st.session_state["api_key"] = api_key[0]
    api_key = st.text_input("API key:", key="api_key", placeholder="sk_live_...")
    st.info(
        "This app never stores your API key. If you have any concerns you can check out the [source code](https://github.com/barnybug/savingsessions) for the app, and please by all means 'Regenerate' your key at the link above after using this."
    )
    if not api_key:
        st.stop()

    if st.experimental_get_query_params().get("api_key") != api_key:
        params = st.experimental_get_query_params() | {"api_key": api_key}
        st.experimental_set_query_params(**params)

    st.info("Tip: bookmark this url to return with your API key remembered.", icon="🔖")

    bar = st.progress(0, text="Authenticating...")

    api = API()
    try:
        api.authenticate(api_key)
    except AuthenticationError:
        error("Authentication error, check your API key")

    bar.progress(0.05, text="Getting account...")
    accounts = api.accounts()
    if not accounts:
        error("No accounts found")
    account = accounts[0]

    debug(account)
    bar.progress(0.1, text="Getting meters...")
    agreements = api.agreements(account.number)
    for agreement in agreements:
        debug(agreement)
    if not agreements:
        error("No agreements on account")

    bar.progress(0.15, text="Getting tariffs...")
    mpans: dict[str, ElectricityMeterPoint] = {}
    for agreement in agreements:
        product = get_product(agreement.tariff.productCode)
        if product.direction in mpans:
            st.warning(
                "Multiple %s meterpoints, using first" % product.direction, icon="⚠️"
            )
        else:
            mpans[product.direction] = agreement.meterPoint
            if len(agreement.meterPoint.meters) > 1:
                st.warning(
                    "Meterpoint %s has multiple meters, using first"
                    % agreement.meterPoint.mpan,
                    icon="⚠️",
                )
        debug(product)

    if meter_point := mpans.get("IMPORT"):
        import_readings = Readings(meter_point)
    else:
        error("Import meterpoint not found")
        raise NotImplementedError()  # unreachable

    if meter_point := mpans.get("EXPORT"):
        export_readings = Readings(meter_point)
    else:
        st.info("Import meter only", icon="ℹ️")
        export_readings = None

    rows = []
    sessions = cache_sessions(api)

    total_ticks = 22

    def tick(message, start, end):
        for i in range(total_ticks):
            bar.progress(start + (end - start) * i / total_ticks, text=message)
            yield
        while True:
            yield

    ticks_per_session = 0.8 / len(sessions)
    for i, ss in enumerate(sessions):
        start = 0.2 + i * ticks_per_session
        ticks = tick(
            f"Getting readings for session #{i+1} ({ss.startAt:%b %d})...",
            start,
            start + ticks_per_session,
        )
        debug(f"session: {ss}")
        row = calculate(
            api, sessions, import_readings, export_readings, ss, ticks, debug
        )
        rows.append(row)

    bar.progress(1.0, text="Done")
    st.subheader("Results")

    st.dataframe(
        rows,
        column_config={
            "session": st.column_config.DatetimeColumn(
                "Session", format="YYYY/MM/DD HH:mm"
            ),
            "import": st.column_config.NumberColumn("Imported", format="%.2f kWh"),
            "export": st.column_config.NumberColumn("Exported", format="%.2f kWh"),
            "baseline": st.column_config.NumberColumn("Baseline", format="%.2f kWh"),
            "saved": st.column_config.NumberColumn("Saved", format="%.2f kWh"),
            "reward": st.column_config.NumberColumn("Reward"),
            "earnings": st.column_config.NumberColumn("Earnings", format="£%.2f"),
        },
    )
    for row in rows:
        if "reward" in row:
            continue
        ts = row["session"]
        st.info(f"Session on {ts:%Y/%m/%d} is awaiting readings...", icon="⌛")


if __name__ == "__main__":
    main()
