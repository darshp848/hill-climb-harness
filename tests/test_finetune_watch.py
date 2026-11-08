"""tools/finetune_watch.py against a scripted fake Dream service (no network, no library, no charges)."""

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import finetune_watch as fw  # noqa: E402


class DreamServerError(Exception):
    def __init__(self, msg, code="", retryable=False, reason="", retry_after_s=None):
        super().__init__(msg)
        self.code, self.retryable, self.reason, self.retry_after_s = code, retryable, reason, retry_after_s


class DreamProtocolError(Exception):
    pass


class FakeService:
    """Each job id maps to a list of states returned one per poll (the last repeats)."""

    def __init__(self, job_scripts, *, create_errors=(), upload_errors=(), dataset_states=("processing", "processed"),
                 existing_jobs=(), warm_script=("ok",), dataset_error=None):
        self.job_scripts = list(job_scripts)
        self.create_errors = list(create_errors)
        self.upload_errors = list(upload_errors)
        self.dataset_states = list(dataset_states)
        self.existing = list(existing_jobs)
        self.warm_script = list(warm_script)
        self.dataset_error = dataset_error
        self.created, self.uploads, self.polls = [], [], {}

    def validate(self, path):
        return "3 episodes"

    def upload(self, path, name):
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        self.uploads.append(name)
        return "ds_1", f"me/{name}"

    def dataset(self, ds_id):
        st = self.dataset_states.pop(0) if len(self.dataset_states) > 1 else self.dataset_states[0]
        return st, "me/relay", self.dataset_error

    def create_job(self, name, suffix):
        if self.create_errors:
            raise self.create_errors.pop(0)
        jid = f"ftj_{len(self.created) + 1}"
        self.created.append((jid, suffix))
        self.polls[jid] = list(self.job_scripts.pop(0))
        return {"id": jid, "status": "queued", "queue_position": 2, "suffix": suffix}

    def job(self, jid):
        q = self.polls[jid]
        st = q.pop(0) if len(q) > 1 else q[0]
        return {"id": jid, **st}

    def jobs(self):
        return list(self.existing)

    def warm(self, model):
        r = self.warm_script.pop(0) if len(self.warm_script) > 1 else self.warm_script[0]
        if isinstance(r, Exception):
            raise r
        return "answered (48 rows)"


def running(step):
    return {"status": "running", "fine_tuned_model": None, "progress": {"phase": "training", "step": step,
                                                                         "total_steps": 1000, "loss": 0.1}}


QUEUED = {"status": "queued", "fine_tuned_model": None, "queue_position": 1}
DONE = {"status": "succeeded", "fine_tuned_model": "dream-1-ft:abc-relay"}
FAIL_TRAIN = {"status": "failed", "fine_tuned_model": None, "error": "the training did not finish"}
FAIL_DATA = {"status": "failed", "fine_tuned_model": None, "error": "episode 3: video has fewer frames than its table"}


def watcher(tmp_path, svc):
    t = [dt.datetime(2026, 11, 7, 22, 0)]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += dt.timedelta(seconds=s)

    w = fw.Watcher(svc, tmp_path / "w", poll_s=300, sleep=sleep, now=lambda: t[0], out=lambda m: None)
    return w, sleeps


def start(w, **kw):
    args = dict(dataset="/data/ft", name="relay", suffix="relay", max_requeues=1, requeue_short=False,
                keep_warm_until=None, warm=True)
    args.update(kw)
    w.start(**args)


def test_happy_path_uploads_queues_and_warms(tmp_path):
    svc = FakeService([[QUEUED, running(100), running(900), {"status": "succeeded", "fine_tuned_model": None}, DONE]])
    w, _ = watcher(tmp_path, svc)
    start(w)
    assert svc.uploads == ["relay"] and svc.created == [("ftj_1", "relay")]
    assert (tmp_path / "w" / "MODEL.txt").read_text().strip() == "dream-1-ft:abc-relay"   # waits for the model id
    assert "warm" in (tmp_path / "w" / "STATUS.txt").read_text()
    assert json.loads((tmp_path / "w" / "state.json").read_text())["warmed"]


def test_training_failure_is_requeued_once_under_a_new_suffix(tmp_path):
    svc = FakeService([[QUEUED, running(400), FAIL_TRAIN], [running(10), DONE]])
    w, _ = watcher(tmp_path, svc)
    start(w)
    assert svc.created == [("ftj_1", "relay"), ("ftj_2", "relay-r2")]
    assert svc.uploads == ["relay"]                                  # same processed dataset, no second upload
    assert w.state["model"] == "dream-1-ft:abc-relay" and w.state["requeues"] == 1


def test_requeue_budget_is_respected(tmp_path):
    svc = FakeService([[FAIL_TRAIN], [FAIL_TRAIN]])
    w, _ = watcher(tmp_path, svc)
    with pytest.raises(fw.Stop, match="budget"):
        start(w)
    assert len(svc.created) == 2
    assert "STOPPED" in (tmp_path / "w" / "STATUS.txt").read_text()


def test_dataset_fault_stops_without_requeue(tmp_path):
    svc = FakeService([[FAIL_DATA]])
    w, _ = watcher(tmp_path, svc)
    with pytest.raises(fw.Stop, match="dataset"):
        start(w)
    assert len(svc.created) == 1


def test_payment_required_stops_before_anything_is_charged(tmp_path):
    svc = FakeService([], create_errors=[DreamServerError("credits do not cover $200", code="payment_required")])
    w, _ = watcher(tmp_path, svc)
    with pytest.raises(fw.Stop, match="payment_required"):
        start(w)
    assert svc.created == []


def test_suffix_in_use_renames_and_network_errors_retry(tmp_path):
    svc = FakeService([[DONE]], create_errors=[DreamProtocolError("connection reset"),
                                               DreamServerError("suffix in use by one of your models", code="conflict"),
                                               DreamServerError("bad gateway", code="http_502", retryable=True)])
    w, sleeps = watcher(tmp_path, svc)
    start(w)
    assert svc.created == [("ftj_1", "relay-r2")] and len(sleeps) >= 2


def test_dataset_name_in_use_is_renamed(tmp_path):
    svc = FakeService([[DONE]], upload_errors=[DreamServerError("a dataset named relay already exists", code="conflict")])
    w, _ = watcher(tmp_path, svc)
    start(w)
    assert svc.uploads == ["relay-r2"]


def test_failed_processing_stops(tmp_path):
    svc = FakeService([], dataset_states=["processing", "failed"], dataset_error="video key missing")
    w, _ = watcher(tmp_path, svc)
    with pytest.raises(fw.Stop, match="failed processing"):
        start(w)
    assert svc.created == []


def test_resume_finds_a_job_queued_before_a_crash_instead_of_queueing_again(tmp_path):
    svc = FakeService([[DONE]])
    w, _ = watcher(tmp_path, svc)
    w.state = {"dataset_path": "/data/ft", "name": "relay", "base_name": "relay", "base_suffix": "relay",
               "suffix": "relay", "suffix_n": 1, "max_requeues": 1, "requeues": 0, "requeue_short": False,
               "keep_warm_until": None, "warm": False, "jobs": [], "dataset_id": "ds_1", "dataset_name": "me/relay"}
    svc.existing = [{"id": "ftj_9", "suffix": "relay", "status": "running"}]
    svc.polls["ftj_9"] = [running(500), DONE]
    w.run()
    assert svc.created == [] and w.state["job_id"] == "ftj_9" and w.state["model"]


def test_stopped_short_keeps_the_model(tmp_path):
    short = {"status": "succeeded", "fine_tuned_model": "dream-1-ft:abc-relay", "notice": "loss became NaN at 700"}
    svc = FakeService([[short]])
    w, _ = watcher(tmp_path, svc)
    start(w, warm=False)
    assert len(svc.created) == 1 and "stopped short" in w.state["status"]


def test_warm_up_waits_through_pool_warming(tmp_path):
    svc = FakeService([[DONE]], warm_script=[DreamServerError("starting", code="DA-2000", retryable=True,
                                                              reason="pool_warming", retry_after_s=120)] * 3 + ["ok"])
    w, sleeps = watcher(tmp_path, svc)
    start(w)
    assert sleeps.count(120) == 3 and w.state["warmed"]


def test_start_refuses_a_second_run_in_the_same_folder(tmp_path):
    svc = FakeService([[DONE]])
    w, _ = watcher(tmp_path, svc)
    start(w, warm=False)
    w2, _ = watcher(tmp_path, FakeService([[DONE]]))
    with pytest.raises(fw.Stop, match="already has a run"):
        start(w2)


def test_cli_refuses_without_accepting_charges(capsys):
    assert fw.main(["start", "/data/ft", "--name", "a", "--suffix", "b"]) == 2
    assert "charges" in capsys.readouterr().err


@pytest.mark.parametrize("exc, kind", [
    (DreamProtocolError("x"), "retry"),
    (DreamServerError("x", code="http_503", retryable=True), "retry"),
    (DreamServerError("not covered", code="payment_required"), "stop"),
    (DreamServerError("knob steps out of range", code="invalid_request"), "stop"),
    (DreamServerError("account at its limit of active jobs", code="conflict"), "busy"),
    (DreamServerError("suffix in use", code="conflict"), "suffix"),
])
def test_classify(exc, kind):
    assert fw.classify(exc) == kind
