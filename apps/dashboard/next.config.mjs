/** @type {import('next').NextConfig} */
const nextConfig = {
  // Operations Console shell (IMP-1101). PAPER-observation only: the shell
  // never talks to a broker and exposes no trading action.
  poweredByHeader: false,
  reactStrictMode: true,
};

export default nextConfig;
