# The frontend image.
#
# The awkward part of deploying this app is that *everything* configurable is
# baked in by `next build`, not read at boot.
#
# NEXT_PUBLIC_* are inlined into the client bundle. Both are public by design
# (the anon key is meant to ship to browsers), so baking them in leaks
# nothing; the constraint is operational.
#
# API_ORIGIN is the same, which is less obvious and was wrong here until a
# container proved it: `rewrites()` is evaluated during the build and its
# destination is frozen into .next/routes-manifest.json, and the standalone
# output does not even include next.config.mjs to re-read. Setting API_ORIGIN
# at runtime does nothing -- the app proxies to whatever it was built with,
# so every API call 500s while both containers report healthy. `next dev`
# reads the config live, which is exactly why this hides in development.
#
# So all three are build arguments, and pointing this image at a different
# backend means rebuilding it.
#
# Vercel handles all of this for you and is the easier target for this app;
# this image exists so the whole stack can be self-hosted.

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# `npm ci` rather than `npm install`: it installs exactly the lockfile and
# fails if package.json disagrees, instead of quietly resolving something new.
RUN npm ci

FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
# Where /api/* is proxied. Defaults to localhost so a plain `docker build`
# still produces a working dev image; set it to the API's address for anything
# real -- http://app:8000 under compose, https://your-api.fly.dev in production.
ARG API_ORIGIN=http://localhost:8000
ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL \
    NEXT_PUBLIC_SUPABASE_ANON_KEY=$NEXT_PUBLIC_SUPABASE_ANON_KEY \
    API_ORIGIN=$API_ORIGIN \
    NEXT_TELEMETRY_DISABLED=1

# Fails the build rather than shipping a bundle with `undefined` baked in,
# where every page throws in the browser and the server looks perfectly
# healthy. This exact mistake cost an afternoon in CI.
RUN test -n "$NEXT_PUBLIC_SUPABASE_URL" \
 || (echo "NEXT_PUBLIC_SUPABASE_URL build-arg is required; see the comment above" && exit 1)
RUN test -n "$NEXT_PUBLIC_SUPABASE_ANON_KEY" \
 || (echo "NEXT_PUBLIC_SUPABASE_ANON_KEY build-arg is required" && exit 1)

RUN npm run build

FROM node:22-slim AS runtime
WORKDIR /app
# HOSTNAME is set explicitly because Docker sets it to the container id, and
# the standalone server reads `process.env.HOSTNAME` as its bind address --
# so without this the server binds to a hostname that changes every run.
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0

RUN useradd --create-home --uid 10001 next

# The standalone output carries its own minimal node_modules; static/ and
# public/ are not included in it and have to come along separately.
COPY --from=builder --chown=next:next /app/.next/standalone ./
COPY --from=builder --chown=next:next /app/.next/static ./.next/static
COPY --from=builder --chown=next:next /app/public ./public

USER next
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
