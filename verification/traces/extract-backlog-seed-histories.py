"""Extract only actual #2778 properties after BOTH full-suite captures finish.

Run from anywhere; reads XML/logs only and writes the data artifact beside this
script. Missing/failed/incomplete runs stop without writing a final artifact.
No focused capture is substituted for a full-suite capture.
"""
from pathlib import Path
import hashlib
import json
import re
import xml.etree.ElementTree as ET

BASE = "a5d79985e66c61bf6a8a569ca7b0b3cf857ae3a2"
EXPECTED = {"same-target-winner-loser", "selected-event-deleted",
            "selected-event-moved", "selected-event-reclassified",
            "opposite-directions", "source-sprint-disappeared",
            "target-sprint-disappeared"}
INPUTS = [("local", "sprintctl.db", "tests/test_backlog_seed_atomicity.py::"),
          ("pg", "sprintctl.pg", "tests/pg/test_backlog_seed_atomicity.py::")]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract():
    inputs, histories = [], []
    for label, backend, prefix in INPUTS:
        xml = Path(f"/tmp/sprintctl-2778-full-{label}.xml")
        log = Path(f"/tmp/sprintctl-2778-full-{label}.txt")
        if not xml.exists() or not log.exists():
            raise RuntimeError(f"unavailable: {label} full XML/log not both present")
        root = ET.parse(xml).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if not suites or any(int(s.get("failures", "0")) or int(s.get("errors", "0")) for s in suites):
            raise RuntimeError(f"unavailable: {label} full XML reports failures/errors")
        contents = log.read_text()
        if not re.search(r"^\d+ passed(?:, .*?)? in [\d.]+s", contents, re.M):
            raise RuntimeError(f"unavailable: {label} terminal success summary missing")
        if label == "pg" and not re.search(r"pg-cleanup-report-check: ok: \d+ fixture cleanup\(s\) across \d+ fixture name\(s\)", contents):
            raise RuntimeError("unavailable: full PG cleanup success not recorded")
        child = []
        for case in root.iter("testcase"):
            for prop in case.findall("./properties/property"):
                if prop.get("name") != "state_history":
                    continue
                observation = json.loads(prop.get("value"))
                if observation.get("backend") != backend or not observation.get("node", "").startswith(prefix):
                    continue
                if case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None:
                    raise RuntimeError("unavailable: a selected history did not pass")
                child.append(observation)
        if len(child) != 7 or {entry["history"] for entry in child} != EXPECTED:
            raise RuntimeError(f"unavailable: {label} expected exactly seven distinct #2778 histories, got {len(child)}")
        inputs.append({"selection": label, "source_base": BASE,
                       "xml": str(xml), "xml_sha256": sha(xml),
                       "log": str(log), "log_sha256": sha(log),
                       "full_run_summary": [line for line in contents.splitlines() if re.match(r"^\d+ passed", line)],
                       "cleanup_summary": [line for line in contents.splitlines() if line.startswith("pg-cleanup-report-check:")]})
        histories.extend(child)
    output = {"kind": "actual pytest record_property ordered histories for sprintctl#2778",
              "source_base": BASE, "captures": inputs,
              "histories_per_backend": 7, "histories": histories,
              "limits": ["Only this child's node identifiers are selected from completed full-suite XML.",
                         "Observations retain actual per-run event/item identifiers, PIDs and order.",
                         "SQLite attempted BEGIN and measured PostgreSQL blocking are distinct observations.",
                         "Opposite-direction completion does not prove all lock orders; sorted SQL is separately source-inspected.",
                         "Deduplication qualification is limited to conforming SQLite writers and measured PostgreSQL READ COMMITTED.",
                         "No focused capture from the earlier 191fc9e base is relabelled as the a5d7998 full run."]}
    destination = Path(__file__).with_name("backlog-seed-histories.json")
    destination.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(destination)
    print(sha(destination))


if __name__ == "__main__":
    extract()
