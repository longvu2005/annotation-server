# Standalone RCR Annotation Server

Two annotation workflows in **one Python standard-library server**:

- **Review:** `/review/` — edit case, Subject→identity assignments, `final_desc`, `final_change`; produces `data/reviewed.jsonl`.
- **Positive Selection:** `/positives/` — select valid candidate target images while retaining the seed; produces `data/positive_sets.jsonl`.

The two annotation pages use the supplied RCR Review and Full Positive Selection layouts. The backend follows the RCR repository's validation and JSONL output contracts. It does **not** require the research repository, Label Studio, Torch, transformers, DINO, or FAFA. Python **3.10+** is sufficient (Ubuntu 22.04/24.04).

## Annotation interface

- Both pages have a task sidebar with progress, All/Pending/Completed tabs, search by task number or sample ID, and a case filter. The browser remembers the last task and filters separately for each workflow.
- Review has case buttons, Query/Target viewports with independent zoom/pan/reset, and clickable identity boxes. S1 is red and S2 is blue. Clicking an identity updates both images; selecting multiple identities for S1 automatically derives GROUP. Keys `1`/`2` switch the active Subject.
- Positives follows the supplied CHANGE-first layout: two columns of target cards with colored Subject boxes, an explicit selection rail, a locked seed, selection counts, and a Selected only toggle. Candidate coordinates are pixels and are scaled against the loaded image's natural size; review coordinates remain percentages.
- Save updates the current task. Save & Next Pending (also `Ctrl/⌘+Enter`) saves successfully before opening the next unfinished task, wrapping to the beginning. Completed tasks remain editable from the Completed tab.
- Unsaved changes are flagged and guarded when switching tasks or leaving the page. Save/load temporarily locks editing to avoid mixing task data. Failed saves keep the current edits.
- Home and the other workflow are available in each sidebar. The pages are self-contained and keep the existing `/review/api/...` and `/positives/api/...` routes, JSONL schema, validation, and low-RAM server unchanged. No new dependencies are required.

## 1. Input/output structure

```text
annotation-server/
  server.py
  static/{index.html,review.html,positives.html,app.js,app.css}
  data/
    images/                    # uploaded images; original split-relative structure
    review_input.jsonl         # upload this to enable Review
    reviewed.jsonl             # generated/updated automatically
    positive_set_input.jsonl   # upload this to enable Positive Selection
    positive_sets.jsonl        # generated/updated automatically
  deploy/{annotation-server.service,nginx.conf}
  tests/test_server.py
  requirements.txt
```

Upload **inputs and images** only. Outputs are created by the web app and can be retrieved after annotation. Empty/missing input files produce an empty task queue rather than a crash. Input JSONL must have the **same schema as the research pipeline's `prepare_review` / `prepare_positives` output**. A `positive_set_input.jsonl` cannot be replaced by `review_input.jsonl`: these are different tasks. Do **not** replace or reorder input tasks during a live annotation batch.

Images may have URLs such as `/PIPA/images/train/abc.jpg` or `/images/train/abc.jpg` or `train/abc.jpg`; upload the referenced image at `data/images/train/abc.jpg`.

**Output compatibility:**

```json
{"sample_id":"...","case_type":"INDIVIDUAL","subjects":[{"subject_id":1,"identity_ids":["..."]}],"final_desc":"Identify Subject 1 as ...","final_change":"then retrieve target images where Subject 1 ..."}
```

```json
{"sample_id":"...","positive_image_ids":["seed_target_id","other_positive_id"]}
```

## 2. Local smoke run

```bash
cd annotation-server
python3 --version
python3 server.py --host 127.0.0.1 --port 8090
```

Visit `http://127.0.0.1:8090/`, `/review/` and `/positives/`. With missing inputs, queues will be empty. Test with `python3 -m unittest discover -s tests -v`.

## 3. Deploy on AWS Lightsail (Ubuntu)

Provision an Ubuntu 22.04 or 24.04 instance. Point a DNS **A record** (e.g. `annotation.example.com`) to its static public IP. In the **Lightsail networking firewall**, allow TCP 22 from your IP and TCP 80/443 from your annotators/the internet. Do **not** open 8090 publicly.

On your Mac/PC after extracting the ZIP:

```bash
# upload package directory (without copying any large data yet)
scp -r annotation-server ubuntu@YOUR_STATIC_IP:/home/ubuntu/

# copy prepared input JSONL and images
scp review_input.jsonl positive_set_input.jsonl ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/
rsync -av --progress ./images/ ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/images/
```

On Lightsail, connected via SSH:

```bash
sudo apt update
sudo apt install -y python3 nginx apache2-utils certbot python3-certbot-nginx
sudo cp /home/ubuntu/annotation-server/deploy/annotation-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now annotation-server
sudo systemctl status annotation-server --no-pager
curl -I http://127.0.0.1:8090/review/
```

Edit `deploy/nginx.conf` to replace `annotation.example.com` with your domain, then:

```bash
sudo cp ~/annotation-server/deploy/nginx.conf /etc/nginx/sites-available/annotation-server
sudo htpasswd -c /etc/nginx/.annotation-users annotator1
# Add a second account without -c (otherwise the original accounts are overwritten):
sudo htpasswd /etc/nginx/.annotation-users annotator2
sudo ln -s /etc/nginx/sites-available/annotation-server /etc/nginx/sites-enabled/annotation-server
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d annotation.example.com
```

**Important:** Basic Auth credentials must only be entered over **HTTPS**. Set up DNS and certbot before giving the URL to annotators. If no domain is available, do not expose this UI over unsecured HTTP; use a trusted VPN or SSH tunnel instead.

Access:
- `https://annotation.example.com/review/`
- `https://annotation.example.com/positives/`

Check logs:

```bash
sudo journalctl -u annotation-server -f
sudo systemctl restart annotation-server
```

## 4. Collect and back up outputs

From your local machine:

```bash
scp ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/reviewed.jsonl ./
scp ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/positive_sets.jsonl ./
```

Make periodic backups of both **output JSONL files** and **input JSONL snapshots**. Each save atomically replaces the output file, preventing partially written lines. Use a persistent Lightsail disk/snapshot and avoid concurrent servers modifying the same data directory.

## 5. Limitations and security

- This is a minimal annotation deployment, **not** a multi-tenant enterprise annotation platform.
- Login is handled by Nginx Basic Auth; the JSONL records are **not stamped with individual annotator usernames**.
- Concurrent editing of the **same sample** uses last-write-wins. Split tasks between annotators and avoid assigning the same sample to different people.
- Any authenticated annotator can navigate/edit both workflows; no per-user access restrictions.
- Uploaded images must reside under `data/images/`. File requests reject parent traversal and symlink escapes.
- The app loads task catalogs at startup; **restart** when uploading new input JSONL. Do **not** replace inputs while annotation is underway, because saved output may become stale.
- If you prefer only Review or only Positives, leave the other input file absent.
