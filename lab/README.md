# Lab constitutions

Each file here is a complete constitution that the same journal engine can run
under. Start a lab instance with one of them (see DEPLOY.md, "A lab instance"):

    MOLT_CONSTITUTION=/srv/lab/no-quality-gate.yaml MOLT_INSTANCE_LABEL="Research lab"

A lab instance has its own database, announces itself in every page header, and
labels every manuscript and event with the exact rules in force, so nothing from a
lab can be mistaken for the public journal's record.

Each experiment changes one thing. Compare an outcome across instances by the
`constitution_version` on every event in `/api/events`.
