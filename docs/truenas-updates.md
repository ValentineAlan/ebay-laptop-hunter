# TrueNAS Apps: GitHub updates with persistent credentials

Keep the TrueNAS app configuration as the deployment authority. GitHub supplies
only application code; it must not supply or replace the live environment values,
host storage paths, browser profile or Product Research cookies.

For an existing custom app, preserve every current service setting and change
only these fields on the two application services:

```yaml
services:
  hunter:
    image: ebay-laptop-hunter:github-main
    pull_policy: build
    build:
      context: https://github.com/ValentineAlan/ebay-laptop-hunter.git#main
      dockerfile: Dockerfile
  sid-helper:
    image: ebay-ebaysid-helper:github-main
    pull_policy: build
    build:
      context: https://github.com/ValentineAlan/ebay-laptop-hunter.git#main
      dockerfile: Dockerfile.ebaysid-helper
```

This is a partial configuration, not a replacement for the complete app YAML.
Keep the existing `environment`, `volumes`, `ports`, `network_mode`, dependencies
and browser service. In particular:

| Setting | Preserve |
| --- | --- |
| Hunter environment | Existing `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` values |
| Hunter and helper `/data` | Same absolute NAS directory, including `hunter.db`, `product-research.curl`, and `ebaysid.current` |
| Browser `/config` | Same absolute NAS directory containing the Chromium profile |
| Helper networking | `network_mode: service:browser` and its existing CDP URL |

The public GitHub repository needs no GitHub token on the NAS. The repository's
`.dockerignore` allows only the application sources and Dockerfiles into local
build contexts. `.gitignore` excludes known credential/session files, databases
and browser profile directories. Ignore rules do not untrack existing files or
protect arbitrary filenames; keep live secrets outside the source checkout.

## Apply an update

Back up the app configuration and persistent directories on the NAS before an
update. Stop the app while copying SQLite databases and the Chromium profile so
the backup is consistent. Restrict backup access: these backups contain secrets.

From the TrueNAS shell, replace the name if your app uses a different name:

```sh
midclt call -j app.redeploy ebay-hunter-stack
```

With `pull_policy: build`, Compose resolves GitHub `main` and rebuilds both custom
images during deployment. A container restart alone does not update the source.
The images above are local build tags, not published Docker Hub images, so use
Redeploy rather than Pull Images for this deployment mode.

There is no background auto-update schedule: a GitHub push becomes live on the
next redeployment. Host storage and existing environment values stay in place.
Use a commit SHA instead of `main` in both context URLs to pin a tested revision.

Afterward, check that all three services are running, the Hunter dashboard shows
the expected version, and Product Research reports a working session. Preserving
the browser profile does not prevent eBay itself from expiring a login.

## Rollback

Retain the previous local image tags and your private NAS backup. Restore the
previous app configuration through TrueNAS. If an update changed the database
schema, stop the app and restore the matching database backup as well. Never
publish a backup or a live app-config export to GitHub.
