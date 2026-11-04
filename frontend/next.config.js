/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  async rewrites() {
    // /api 代理到后端。容器内后端服务名为 api（Docker bridge 网络 DNS），
    // 通过 NEXT_PUBLIC_API_URL 可覆盖（本地开发时指向 http://localhost:8000）。
    return [
      {
        source: '/api/:path*',
        destination: `${process.env.NEXT_PUBLIC_API_URL || 'http://api:8000'}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
