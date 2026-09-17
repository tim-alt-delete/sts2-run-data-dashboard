# Running

```bash
cd ~/slay-the-spire-mod/dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python sts2data.py            # ← the real check
flask --app app run --debug   # http://127.0.0.1:5000
```