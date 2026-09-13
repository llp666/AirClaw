import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * 静态导出：构建产物是一组纯静态文件，不再需要 Node 运行时。
   *
   * 本项目前端是纯客户端 SPA（所有数据经 fetch 打后端 8002，无服务端专有 API、
   * 无动态路由），因此可以完全静态化。这样部署机只需 Python + Docker，
   * 产物由后端 FastAPI 直接托管，且前后端同源后不再需要 CORS。
   */
  output: "export",
  /** 导出时生成 index.html 形式的目录结构，便于静态服务器正确解析路径。 */
  trailingSlash: true,
  /** 静态导出不支持 Next 的图片优化服务，明确关闭以免构建报错。 */
  images: { unoptimized: true },
};

export default nextConfig;
