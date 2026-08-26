from ragnarok.runner import _EtaJobPlan, _EtaRolePlan, _SuiteEtaProgress


def test_suite_eta_includes_current_and_future_jobs(monkeypatch):
    events = []
    now = [0.0]
    monkeypatch.setattr("ragnarok.runner.time.perf_counter", lambda: now[0])
    tracker = _SuiteEtaProgress(
        lambda *args: events.append(args),
        [
            _EtaJobPlan(("model", "first"), {"subject": _EtaRolePlan(100)}),
            _EtaJobPlan(("model", "second"), {"subject": _EtaRolePlan(100)}),
        ],
    )
    tracker.start_job(("model", "first"))
    tracker.update("inference", 0, 100, "starting")
    now[0] = 10.0

    tracker.update("inference", 50, 100, "case 50", {"tokens_per_second": 50.0})

    stats = events[-1][4]
    assert stats["eta_seconds"] == 30.0
    assert stats["eta_label"] == "Suite ETA"
    assert stats["tokens_per_second"] == 50.0


def test_suite_eta_tracks_subject_and_judge_as_separate_required_work(monkeypatch):
    events = []
    now = [0.0]
    monkeypatch.setattr("ragnarok.runner.time.perf_counter", lambda: now[0])
    tracker = _SuiteEtaProgress(
        lambda *args: events.append(args),
        [_EtaJobPlan(("model", "mpib"), {
            "subject": _EtaRolePlan(100),
            "judge": _EtaRolePlan(100),
        })],
    )
    tracker.start_job(("model", "mpib"))
    tracker.update("subject_inference", 0, 100, "subject starting")
    now[0] = 20.0

    tracker.update("subject_inference", 100, 100, "subject complete")

    # Until the Judge produces samples, it uses its own conservative default
    # instead of incorrectly inheriting Subject throughput.
    assert events[-1][4]["eta_seconds"] == 150.0
    tracker.update("judge_inference", 0, 100, "judge starting")
    now[0] = 30.0
    tracker.update("judge_inference", 50, 100, "judge 50")
    assert events[-1][4]["eta_seconds"] == 10.0


def test_suite_eta_resume_uses_new_progress_as_its_timing_baseline(monkeypatch):
    events = []
    now = [0.0]
    monkeypatch.setattr("ragnarok.runner.time.perf_counter", lambda: now[0])
    tracker = _SuiteEtaProgress(
        lambda *args: events.append(args),
        [_EtaJobPlan(("model", "mpib"), {"subject": _EtaRolePlan(100)})],
    )
    tracker.start_job(("model", "mpib"))
    tracker.update("subject_inference", 80, 100, "resumed at 80")
    assert events[-1][4]["eta_seconds"] is None

    now[0] = 10.0
    tracker.update("subject_inference", 90, 100, "case 90")

    assert events[-1][4]["eta_seconds"] == 10.0


def test_suite_eta_applies_framework_weight_and_parallelism(monkeypatch):
    events = []
    now = [0.0]
    monkeypatch.setattr("ragnarok.runner.time.perf_counter", lambda: now[0])
    tracker = _SuiteEtaProgress(
        lambda *args: events.append(args),
        [
            _EtaJobPlan(("model", "spikee"), {"subject": _EtaRolePlan(100)}),
            _EtaJobPlan(("model", "mpib"), {"subject": _EtaRolePlan(100, task_weight=2.0)}),
            _EtaJobPlan(("model", "judged"), {"judge": _EtaRolePlan(100, parallelism=4)}),
        ],
    )
    tracker.start_job(("model", "spikee"))
    tracker.update("subject_inference", 0, 100, "starting")
    now[0] = 10.0
    tracker.update("subject_inference", 50, 100, "case 50")

    # 10 seconds for active SPIKEE + 40 seconds for weighted MPIB +
    # 37.5 seconds for the unseen Judge default divided by four workers.
    assert events[-1][4]["eta_seconds"] == 87.5


def test_suite_eta_uses_a_cumulative_rate_instead_of_overweighting_one_slow_case(monkeypatch):
    events = []
    now = [0.0]
    monkeypatch.setattr("ragnarok.runner.time.perf_counter", lambda: now[0])
    tracker = _SuiteEtaProgress(
        lambda *args: events.append(args),
        [_EtaJobPlan(("model", "spikee"), {"subject": _EtaRolePlan(100)})],
    )
    tracker.start_job(("model", "spikee"))
    tracker.update("subject_inference", 0, 100, "starting")
    now[0] = 10.0
    tracker.update("subject_inference", 50, 100, "case 50")
    now[0] = 20.0
    tracker.update("subject_inference", 51, 100, "one unusually slow case")

    # All 51 observed cases contribute proportionally: 20 seconds / 51 cases.
    assert tracker.seconds_per_work[("model", "subject")] == 20 / 51
    assert events[-1][4]["eta_seconds"] == (20 / 51) * 49


def test_suite_eta_recalibrates_planned_calls_to_reported_progress_units(monkeypatch):
    events = []
    now = [0.0]
    monkeypatch.setattr("ragnarok.runner.time.perf_counter", lambda: now[0])
    tracker = _SuiteEtaProgress(
        lambda *args: events.append(args),
        [
            _EtaJobPlan(("q2", "agentdojo"), {"subject": _EtaRolePlan(1_600)}),
            _EtaJobPlan(("q4", "agentdojo"), {"subject": _EtaRolePlan(1_600)}),
        ],
    )
    tracker.start_job(("q2", "agentdojo"))
    tracker.update("inference", 0, 100, "starting")
    now[0] = 10.0
    tracker.update("inference", 10, 100, "case 10")

    # AgentDojo reports completed cases, not its maximum possible LLM calls.
    # The observed total applies to later quantizations of the same benchmark.
    assert events[-1][4]["eta_seconds"] == 190.0
