# Official S&P announcement documents (owner-supplied)

Drop official S&P Dow Jones Indices index-change announcements here, then run:

```bash
bash scripts/run_dataset_v1_real_qualification.sh
```

The runner discovers, checksums and parses them and **generates the manifest
itself** — there is no manifest to author by hand and no cleanup step.

Supported: `.html` / `.htm`, `.txt`, `.csv`, `.tsv`, `.json`, and `.pdf` with a
text layer. A scanned PDF is reported as unparsed rather than guessed at.

Why manual: `spglobal.com` returns HTTP 403 to plain automated requests
(including for `robots.txt`). That is publisher bot protection and this project
does not circumvent it. Documents you download yourself are fine.

Extraction is deliberately strict: only values beside an **explicit** label are
read, and two different dates under one label fail closed. Each document's
findings say exactly why it was or was not usable.

**Do not commit licensed or redistribution-restricted documents.** Only the
sha256 digest, filename and parsed fields travel into the evidence; the document
content itself is never copied into an artifact.
