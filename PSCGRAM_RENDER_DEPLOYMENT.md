# PSCGram Quizbot — Render Free deployment

This package is prepared to deploy the existing Quizbot project as one Render
Docker Web Service. It runs the creator bot, runner bot and FastAPI Mini App
together. The FastAPI app already exposes `/healthz`; Render uses that path
for its health check. If `MINI_APP_DOMAIN` is not explicitly set, the app now
uses Render's `RENDER_EXTERNAL_URL`.

## Before deploying

You need:
- Two Telegram bot tokens (creator and runner; they can be the same token only
  if you intentionally configure the project that way).
- Telegram API ID and API hash from https://my.telegram.org.
- Your Telegram numeric user ID for `OWNER_ID`.
- A MongoDB Atlas connection string.

## Deploy

1. Upload this folder to a new GitHub repository. Do not upload a real `.env`
   file or put tokens in source code.
2. In Render, choose **New + → Blueprint** and connect that GitHub repository.
3. Render reads `render.yaml` and creates the `pscgram-quizbot` web service.
4. Enter the secret values when Render asks for them:
   `API_ID`, `API_HASH`, `CREATOR_BOT_TOKEN`, `RUNNER_BOT_TOKEN`,
   `MONGODB_URI`, and `OWNER_ID`.
5. Deploy. Check the Render logs for successful MongoDB connection and both
   bots starting.
6. Open `https://YOUR-RENDER-SERVICE.onrender.com/healthz`. A healthy service
   should return `{"ok":true}`.

## UptimeRobot

1. Create an HTTP(s) monitor in UptimeRobot.
2. Monitor URL: `https://YOUR-RENDER-SERVICE.onrender.com/healthz`
3. Select a 5-minute interval (the free tier's usual minimum; check the
   current UptimeRobot plan limits).
4. Confirm the monitor reports **Up**.

A periodic HTTP request can reset Render's inactivity timer, but this is a
workaround, not a guarantee of uninterrupted free hosting. Render may restart
services, impose free-instance limits, or change plan rules. Check the current
Render and UptimeRobot terms before relying on this for production.

## Important notes

- This is a single free web service, not a separate Render background worker.
- Render's filesystem is ephemeral. The project is configured to use MongoDB
  for persistent quiz/user data; do not rely on local files for durable storage.
- The Mini App is enabled using Render's public HTTPS URL. Set `MINI_APP_DOMAIN`
  explicitly only if you later use a custom domain.
- Razorpay payments remain disabled until valid Razorpay credentials are
  configured. `FREE_BOT=true` marks users as premium according to the project's
  existing feature logic; review access settings before making paid quizzes.
- AI generation requires the relevant provider credentials/configuration in
  the bot's supported settings. No AI API key is included in this package.
- Do not expose Telegram tokens, API hashes, MongoDB credentials, or payment
  secrets in GitHub, screenshots, or chat.

## Scope of validation

The Python source files were syntax-compiled after modification. This does
not prove live Telegram authentication, MongoDB connectivity, AI provider
calls, payments, or all bot workflows. Those require your real credentials
and deployment-time testing.
