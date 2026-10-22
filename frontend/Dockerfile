# AI4S 学术研究 Agent 前端镜像
# 关键设计决策：使用 Next.js standalone 输出，产物只包含运行所需依赖，
# 镜像体积小、启动快，适合云端与本地一键复现。
FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

# 第二阶段：仅复制 standalone 产物
FROM node:20-alpine AS runner

WORKDIR /app

# 安装 curl 用于 docker healthcheck
RUN apk add --no-cache curl

ENV NODE_ENV=production
ENV PORT=3000

COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
