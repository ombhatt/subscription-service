/** @type {import('next').NextConfig} */
const API = process.env.API_ORIGIN ?? "http://localhost:8000";

const nextConfig = {
  // Emit a self-contained server bundle with only the node_modules actually
  // reached at runtime, so the production image copies ~50MB instead of the
  // full dependency tree. Required by web/Dockerfile.
  output: "standalone",

  // Proxy the API through this origin instead of calling localhost:8000 from
  // the browser. No CORS configuration, no preflight, and it matches how you
  // would deploy this behind a single domain. If you ever host the frontend
  // separately, drop this and add CORS middleware to the FastAPI app instead.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/:path*` }];
  },
};

export default nextConfig;
