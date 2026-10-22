# AI4S 学术研究 Agent 本地运维命令
# 关键设计决策：用 Makefile 封装常用 docker compose 操作与一键健康检查，
# 让评审与本地开发只需记住 make up / down / eval 三个命令。

.PHONY: help up down build logs eval ps clean

help: ## 显示可用命令
	@echo "AI4S Academic Agent - 可用命令:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up: ## 一键构建并启动全部 6 个服务
	docker compose up --build -d

down: ## 停止并移除全部服务
	docker compose down

build: ## 仅构建镜像，不启动
	docker compose build

logs: ## 查看所有服务日志（跟随）
	docker compose logs -f

ps: ## 查看服务运行状态
	docker compose ps

eval: ## 一键验收：健康检查 + golden QA 评测 + 生成 Markdown 报告
	@echo "==> 等待服务 healthy..."
	@for i in $$(seq 1 30); do \
		if [ "$$(docker compose ps --format json | grep -c healthy)" = "6" ]; then \
			echo "==> 6 个服务全部 healthy"; \
			break; \
		fi; \
		sleep 3; \
	done
	@echo "==> 测试后端 /health"
	@curl -sf http://localhost:8000/api/v1/health/ > /dev/null && echo "  API /health: 200 OK" || (echo "  API /health: FAILED"; exit 1)
	@echo "==> 测试前端首页"
	@curl -sf http://localhost:3000/ > /dev/null && echo "  Web home: 200 OK" || (echo "  Web home: FAILED"; exit 1)
	@echo "==> 运行 golden QA 评测（结果写入 backend/eval/reports/）"
	@docker compose exec -T api python /app/scripts/eval_runner.py
	@echo "==> 验收通过"

clean: ## 清理容器、卷与镜像（危险，需确认）
	@echo "即将删除容器、卷和构建缓存..."
	@read -p "确认? [y/N] " ans && [ "$$ans" = "y" ] && docker compose down -v --rmi local || echo "已取消"
