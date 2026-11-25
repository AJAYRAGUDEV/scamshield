# Deploying ScamShield on Render

ScamShield is **one** web service: the FastAPI server serves the API and the web page together. Render builds it from the
`Dockerfile` (the Docker runtime is needed because video validation uses **ffprobe**, which Render's plain Python runtime does
not include). `render.yaml` describes the service so you can create it in a few clicks.

> Status: the files below were prepared and the start command was tested locally in production mode, but **they have not been
> deployed or built with Docker on this machine** (Docker is not installed here). Treat the first deployment as the real test and
> follow the checklist in section 4.

## 1. Free plan or always-on

`render.yaml` is set to the **free** plan, which needs no payment method but **sleeps**: after about 15 minutes with no visitors it
spins down and the next request waits roughly a minute while it starts. Running video jobs are lost when it sleeps. The free instance
also has little memory and CPU, so video analysis is slower, and Render limits free usage per month (check their current terms).

To stop it sleeping, switch to a paid type (section below): in the dashboard open the service, **Settings > Instance Type > Starter**,
or edit `plan:` in `render.yaml` and push. An external "uptime pinger" can keep a free instance awake in practice, but it is a
workaround: it is not guaranteed, it still restarts the service at times, and it uses your free monthly hours.

### Keeping a free instance awake with UptimeRobot

UptimeRobot (free) can request your site every 5 minutes, which is shorter than Render's 15-minute idle limit, so the instance should
not spin down. Set it up after the service is live:

1. Create a free account at uptimerobot.com and click **Add New Monitor**.
2. **Monitor Type:** HTTP(s). **Friendly Name:** ScamShield. **URL:** `https://<your-service>.onrender.com/health`.
3. **Monitoring Interval:** 5 minutes (the free minimum). Add your e-mail as the alert contact, then create the monitor.
4. The monitor should turn **Up** within a minute or two. `/health` accepts `GET` and `HEAD`, because free UptimeRobot probes with `HEAD`.

What this does **not** fix: it is a workaround and not a guarantee. A redeploy, a crash or Render's own maintenance still restarts the
service and drops running video jobs; one month has about 744 hours, which a single always-on free service just fits inside Render's
free allowance (check Render's current terms); and if UptimeRobot changes its free plan the effect stops. The monitor also tells you
when the site is down, which is useful in itself.

## 1b. Keeping the API awake (paid)

Render's **free** web services spin down after about 15 minutes without traffic and take a while to start again on the next
request. A sleeping service is slow for the first visitor and, because ScamShield keeps video jobs in memory, a restart also loses
any running video analysis.

The reliable fix is a **paid instance type** (`plan: starter` in `render.yaml`), which keeps the service running. Check Render's
pricing page for the current cost and for larger sizes (analysing video is the heaviest work; if it feels slow or runs out of
memory, move up one size in the dashboard).

A free instance kept awake with an external "pinger" (UptimeRobot, above) is a workaround, not a guarantee.

## 2. One-time setup

You need a Render account, a GitHub (or GitLab) account and a Gemini API key.

1. **Put the project in a private Git repository.** This folder is not a repository yet. In PowerShell, from the project folder:

   ```powershell
   git init
   git add .
   git status            # confirm that .env is NOT listed; if it is, stop and fix .gitignore first
   git commit -m "ScamShield"
   ```

   Create an empty **private** repository on GitHub and push to it (GitHub shows the exact `git remote add` / `git push`
   commands). Never commit `.env`: it contains your API key. `.gitignore` already excludes it.

2. **Create the service.** In Render: **New > Blueprint**, choose the repository, and Render reads `render.yaml`. When asked for
   `GEMINI_API_KEY`, paste your key there (it is stored by Render, not in the repository). Apply the Blueprint.

   Without a Blueprint: **New > Web Service**, runtime **Docker**, instance type **Free** (or **Starter** to stay awake), **Health Check Path**
   `/health`, and add the environment variables below.

3. **Wait for the first build** (a few minutes: it installs FFmpeg and the Python packages). The service URL looks like
   `https://scamshield-xxxx.onrender.com`.

## 3. Settings

| Variable | Value on Render | Notes |
|---|---|---|
| `GEMINI_API_KEY` | _your key_ | Secret. Set in the Render dashboard only |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Change it here if Google retires the model |
| `APP_ENV` | `production` | Turns off the interactive `/docs` pages |
| `TRUSTED_PROXY_IPS` | private ranges (see `render.yaml`) | Lets the rate limiter see each visitor's address instead of Render's proxy |
| `PORT` | set by Render | The Dockerfile listens on it |

Everything else keeps its default (see the README configuration table). `FFPROBE_PATH` is not needed: FFmpeg is installed in the
image and `ffprobe` is on `PATH`.

**Keep exactly one instance** (`numInstances` 1; the free plan always has one). Video jobs and rate-limit counters live in memory and are not shared, so a
second instance would give users "job not found" errors and double the allowed request rate.

## 4. After the first deployment: check it

1. Open `https://<your-service>.onrender.com/health`: it should show `{"status":"ok"}`.
2. Open the site root, switch to **FastAPI Live** (the "Backend" drawer should say *Ready*), run a text example.
3. Try an audio clip, then a short video (the Video Clip tab). A video job should reach `completed` or `unable_to_assess`.
4. In the Render **Logs**, look for the start-up line `gemini_configured=True`. If you see `ffprobe was not found`, the image was not
   built as intended (video uploads would be refused with `VIDEO_VALIDATION_UNAVAILABLE`).
5. **Rate limits per visitor.** The limits assume Render forwards the visitor's address in `X-Forwarded-For` and that its proxy is
   inside `TRUSTED_PROXY_IPS`. This was **not verifiable without a deployment**. Check it: submit four video uploads quickly from
   your computer (the limit is 3 per minute) and expect `429` on the fourth, then try one from a phone on mobile data and expect it
   to work. If visitors block each other, the proxy address is outside the configured ranges: widen `TRUSTED_PROXY_IPS`. If the
   limit never triggers, the setting is not being applied. A wrong setting never lets a client choose its own identity, it only
   changes how requests are grouped.
6. **Large uploads.** The server accepts up to 100 MB, but whether Render's network layer accepts a body that large on your plan was
   not verified. Test with a video of about 50-100 MB before you rely on it.

## 5. What to know before sharing the link

* **Anyone with the URL can use your Gemini quota.** There is no login. The built-in limits (3 video, 10 audio and 30 text analyses
  per minute per visitor, 120 per minute overall) reduce abuse but do not stop a determined one. Set a spending/quota limit on the
  Gemini key in Google AI Studio or Cloud and watch usage in the first days.
* **Jobs are lost on restart.** Every deploy, crash or manual restart drops running video analyses (the page then reports the job
  could not be found). Avoid deploying while people are using it.
* Uploaded videos are deleted after each analysis, locally and at Gemini; the container's disk is temporary anyway.
* The page loads Tailwind, Chart.js and Lucide from public CDNs, so visitors need internet access.
* Responsible use: results are indicators, not proof; see the README.

## 6. Operations

* **Update:** push to the connected branch; Render rebuilds and redeploys (turn auto-deploy off in the dashboard if you prefer
  manual deploys).
* **Rotate the key:** create a new key in Google, change `GEMINI_API_KEY` in Render (it restarts the service), then delete the old key.
* **Roll back:** use the **Rollback** option on a previous deploy in the Render dashboard.
* **Logs:** Render's Logs tab. Logs redact the API key and contain no uploaded content, but do contain request paths and job ids
  (shortened).
