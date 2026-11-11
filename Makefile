# AI4S 学术研究 Agent 本地运维命令
# 关键设计决策：用 Makefile 封装常用 docker compose 操作与一键健康检查，
# 让评审与本地开发只需记住 make up / down / eval 三个命令。

.PHONY: help up down build logs eval eval-full ps clean

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

eval: ## 一键验收：健康检查 + 检索基线评测（不调 LLM，约 90 秒）
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
	@echo "==> 校验评测集（gold 引句能否在库内命中 / 不可答词是否真零命中）"
	@docker compose exec -T api python /app/eval/baseline/run_baseline.py validate
	@echo "==> 检索基线评测：五个配置逐题跑，不调 LLM（结果写入 backend/eval/baseline/runs/）"
	@docker compose exec -T api python /app/eval/baseline/run_baseline.py run --no-generate
	@echo "==> 验收通过（报告见 backend/eval/baseline/runs/<run_id>/report.md）"

eval-full: ## 完整评测：含生成与引用抽检（调用 LLM，约 4.5 分钟）
	@docker compose exec -T api python /app/eval/baseline/run_baseline.py run
	@echo "==> 可用 verify <run_id> 重放检索、逐题比对"

clean: ## 清理容器、卷与镜像（危险，需确认）
	@echo "即将删除容器、卷和构建缓存..."
	@read -p "确认? [y/N] " ans && [ "$$ans" = "y" ] && docker compose down -v --rmi local || echo "已取消"
