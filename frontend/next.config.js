/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // 构建目录可用环境变量改：本机反复构建时 .next/trace 偶尔被占用（Windows 上
  // 会报 EPERM），换个目录就绕开了；容器里不设这个变量，行为不变。
  distDir: process.env.NEXT_DIST_DIR || '.next',
  experimental: {
    // Next 的 rewrites 代理默认只等 30s（next/dist/server/lib/router-utils/proxy-request.js
    // 里 `proxyTimeout || 30000` 的硬编码），到点就 abort，前端拿到 ECONNRESET → 500。
    // 综述生成要跑「抽 claim → 归并 → 判冲突 → 写正文」四次模型调用，实测 44~90s，
    // 走 :3000 必然失败（走 deploy/nginx.conf 的网关不受影响，那里是直连 api）。
    // 这里放宽到 180s：低于网关的 300s，保证两条入口行为一致。
    proxyTimeout: 180000,
  },
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
