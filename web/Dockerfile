# The frontend image.
#
# The awkward part of deploying this app is that NEXT_PUBLIC_* variables are
# inlined into the client bundle by `next build`, not read at boot. So the
# Supabase URL and anon key are build arguments, and an image built for one
# Supabase project cannot be pointed at another by changing its environment --
# it has to be rebuilt. Both values are public by design (the anon key is
# meant to ship to browsers), so baking them in leaks nothing; the constraint
# is operational, not a security one. API_ORIGIN, by contrast, is used by the
# rewrite at request time and *is* a runtime variable.
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
ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL \
    NEXT_PUBLIC_SUPABASE_ANON_KEY=$NEXT_PUBLIC_SUPABASE_ANON_KEY \
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
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000

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
