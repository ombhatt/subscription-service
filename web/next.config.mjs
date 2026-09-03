/** @type {import('next').NextConfig} */
const API = process.env.API_ORIGIN ?? "http://localhost:8000";

const nextConfig = {
  // Proxy the API through this origin instead of calling localhost:8000 from
  // the browser. No CORS configuration, no preflight, and it matches how you
  // would deploy this behind a single domain. If you ever host the frontend
  // separately, drop this and add CORS middleware to the FastAPI app instead.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/:path*` }];
  },
};

export default nextConfig;
