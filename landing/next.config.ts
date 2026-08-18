import type { NextConfig } from "next";

const isGitHubPages = process.env.GITHUB_PAGES === "true";
const githubPagesBasePath = "/hypercopy";

const nextConfig: NextConfig = {
  output: isGitHubPages ? "export" : undefined,
  basePath: isGitHubPages ? githubPagesBasePath : undefined,
  assetPrefix: isGitHubPages ? githubPagesBasePath : undefined,
  trailingSlash: isGitHubPages,
  images: { unoptimized: true },
};

export default nextConfig;
