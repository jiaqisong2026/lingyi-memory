# -*- coding: utf-8 -*-
"""BM25 tokenization compatibility layer.

Reuses the synonym expansion and CJK bigram logic from the original
bm25_scorer.py but as a standalone module (no class overhead).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Set

_CJK_RANGE = re.compile(r'[一-鿿㐀-䶿]')


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RANGE.match(ch))


def _cjk_bigrams(text: str) -> List[str]:
    cjk_chars = [ch for ch in text if _is_cjk(ch)]
    if len(cjk_chars) < 2:
        return cjk_chars if cjk_chars else []
    return [cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)]


def _cjk_char_tokens(text: str) -> List[str]:
    return [ch for ch in text if _is_cjk(ch)]


# Synonym map — copied from bm25_scorer.py SYNONYM_MAP
SYNONYM_MAP: Dict[str, List[str]] = {
    "定时": ["cron", "crontab", "schedule", "timer"],
    "容器": ["docker", "container", "pod", "k8s", "kubernetes"],
    "部署": ["deploy", "docker", "kubernetes", "k8s", "ci", "cd"],
    "数据库": ["mysql", "postgres", "postgresql", "redis", "mongodb", "sqlite"],
    "缓存": ["redis", "memcached", "cache", "cdn"],
    "端口": ["port", "80", "443", "3306", "5432", "6372"],
    "反向代理": ["nginx", "apache", "caddy", "proxy", "reverse"],
    "版本控制": ["git", "svn", "github", "gitlab", "vcs"],
    "测试": ["test", "pytest", "unittest", "jest", "coverage"],
    "日志": ["log", "logging", "elk", "grafana", "journalctl"],
    "监控": ["prometheus", "grafana", "monitoring", "alert", "metrics"],
    "安全": ["security", "ssl", "tls", "https", "oauth", "jwt", "firewall", "iptables"],
    "防火墙": ["firewall", "iptables", "ufw", "security"],
    "虚拟环境": ["venv", "virtualenv", "conda", "poetry"],
    "格式化": ["format", "black", "prettier", "lint", "eslint"],
    "性能": ["performance", "optimization", "benchmark", "profil"],
    "优化": ["optimize", "performance", "tuning", "improve"],
    "备份": ["backup", "restore", "rsync", "snapshot"],
    "认证": ["auth", "oauth", "jwt", "token", "login"],
    "消息队列": ["rabbitmq", "kafka", "mq", "queue", "celery"],
    "微服务": ["microservice", "grpc", "api", "gateway", "service", "architecture"],
    "负载均衡": ["loadbalance", "nginx", "haproxy", "lb"],
    "编辑器": ["vim", "nvim", "vscode", "emacs", "editor"],
    "脚本": ["script", "shell", "bash", "python"],
    "安装": ["install", "apt", "yum", "pip", "brew"],
    "配置": ["config", "configure", "setup", "setting"],
    "服务": ["service", "server", "daemon", "systemd"],
    "网络": ["network", "tcp", "udp", "http", "dns"],
    "文件": ["file", "filesystem", "rsync", "scp"],
    "进程": ["process", "pid", "systemd", "service"],
    "内存": ["memory", "ram", "swap", "oom"],
    "磁盘": ["disk", "storage", "ssd", "hdd", "mount"],
    "持续集成": ["ci", "jenkins", "gitlab", "github", "pipeline", "build"],
    "持续部署": ["cd", "deploy", "delivery", "pipeline", "release"],
    "代码审查": ["code review", "pull request", "pr", "merge request", "review"],
    "审查": ["review", "pull request", "pr", "merge request", "code review"],
    "数据一致性": ["acid", "transaction", "consistency", "事务"],
    "一致性": ["consistency", "acid", "transaction", "事务"],
    "事务": ["transaction", "acid", "commit", "rollback"],
    "架构": ["architecture", "design", "pattern", "microservice"],
    "设计原则": ["design", "principle", "pattern", "solid"],
    "框架": ["framework", "library", "sdk", "flask", "django", "fastapi", "react", "vue"],
    "人工智能": ["ai", "llm", "agent", "langchain", "machine learning"],
    "攻击": ["ddos", "security", "attack", "firewall", "intrusion"],
    "被攻击": ["ddos", "security", "attack", "firewall", "security"],
    "崩溃": ["crash", "crash", "故障", "down", "挂了", "宕机", "异常"],
    "卡": ["slow", "lag", "性能", "performance", "延迟", "timeout"],
    "卡了": ["slow", "hang", "freeze", "性能问题", "数据库"],
    "慢": ["slow", "latency", "性能", "optimization"],
    "挂了": ["down", "crash", "故障", "offline"],
    "崩了": ["crash", "down", "异常", "oom"],
    "打不开": ["unreachable", "timeout", "网络", "dns"],
    "不通": ["unreachable", "timeout", "network", "firewall"],
    "排查": ["debug", "troubleshoot", "诊断", "分析"],
    "健康检查": ["health", "check", "probe", "liveness", "readiness"],
    "灰度": ["canary", "灰度发布", "渐进式", "金丝雀"],
    "ssl": ["ssl", "tls", "https", "证书", "certificate"],
    "证书": ["certificate", "ssl", "tls", "https"],
    "sql注入": ["sql injection", "注入", "安全", "参数化", "prepared"],
    "注入": ["injection", "sql", "安全", "xss"],
    "主从": ["master", "slave", "replication", "主从复制"],
    "全文": ["fulltext", "全文搜索", "索引"],
    "减小体积": ["docker", "镜像", "优化", "multi-stage", "slim"],
    # English → bidirectional
    "ssh": ["ssh", "远程", "remote", "端口", "22"],
    "docker": ["docker", "容器", "container", "镜像", "image"],
    "python": ["python", "pip", "pytest", "虚拟环境"],
    "nginx": ["nginx", "反向代理", "proxy", "负载均衡"],
    "redis": ["redis", "缓存", "cache", "nosql"],
    "mysql": ["mysql", "数据库", "database", "sql"],
    "postgres": ["postgresql", "数据库", "database", "sql"],
    "postgresql": ["postgresql", "数据库", "database", "sql"],
    "git": ["git", "版本控制", "branch", "merge"],
    "cron": ["cron", "crontab", "定时", "schedule"],
    "vim": ["vim", "nvim", "编辑器", "editor"],
    "k8s": ["kubernetes", "k8s", "容器", "pod"],
    "kubernetes": ["kubernetes", "k8s", "容器", "pod", "container"],
    "ci": ["ci", "持续集成", "jenkins", "gitlab", "github", "pipeline"],
    "cd": ["cd", "持续部署", "deploy", "delivery"],
    "api": ["api", "接口", "restful", "graphql", "grpc"],
    "https": ["https", "ssl", "tls", "证书", "certificate"],
    "oauth": ["oauth", "认证", "auth", "token"],
    "jwt": ["jwt", "token", "认证", "auth"],
    "elk": ["elk", "elasticsearch", "日志", "log"],
    "grafana": ["grafana", "监控", "dashboard"],
    "prometheus": ["prometheus", "监控", "metrics", "alert"],
    "json": ["json", "格式", "format"],
    "yaml": ["yaml", "配置", "config"],
    "tcp": ["tcp", "网络", "network", "传输"],
    "udp": ["udp", "网络", "network", "传输"],
    "dns": ["dns", "域名", "domain", "解析"],
    "http": ["http", "协议", "request", "response"],
    "ddos": ["ddos", "分布式拒绝服务", "攻击", "防护", "cloudflare"],
    "ai": ["ai", "人工智能", "llm", "agent", "langchain"],
    "llm": ["llm", "ai", "人工智能", "language model"],
    "agent": ["agent", "智能体", "ai", "assistant"],
    "review": ["review", "审查", "pull request", "pr"],
    "security": ["security", "安全", "firewall", "防火墙", "attack"],
    "performance": ["performance", "性能", "optimization", "优化"],
    "framework": ["framework", "框架", "library", "sdk"],
    "acid": ["acid", "事务", "transaction", "数据一致性"],
    "transaction": ["transaction", "事务", "acid", "commit"],
    "front": ["前端", "frontend", "react", "vue", "angular"],
    "back": ["后端", "backend", "api", "server"],
    "microservice": ["microservice", "微服务", "grpc", "api", "gateway"],
    "pipeline": ["pipeline", "流水线", "ci", "cd", "jenkins"],
    "code": ["code", "代码", "source", "program"],
    "editor": ["editor", "编辑器", "vim", "nvim", "vscode", "emacs"],
    "sre": ["sre", "可靠性", "站点可靠性", "site reliability", "error budget", "sla", "slo"],
    "错误预算": ["error budget", "sre", "sla", "slo", "预算"],
    "sla": ["sla", "sre", "服务级别", "协议", "agreement"],
    "slo": ["slo", "sre", "目标", "objective"],
    "混沌工程": ["chaos", "chaos engineering", "故障注入", "fault injection"],
    "金丝雀": ["canary", "canary release", "灰度发布", "金丝雀发布"],
    "蓝绿": ["blue", "green", "蓝绿部署", "blue-green"],
    "熔断": ["circuit breaker", "熔断器", "断路器", "hystrix", "resilience"],
    "限流": ["rate limit", "rate limiting", "限流", "throttle", "令牌桶", "漏桶"],
    "可观测性": ["observability", "metrics", "logs", "traces", "监控"],
    "链路追踪": ["tracing", "distributed tracing", "jaeger", "zipkin", "skywalking"],
    "服务网格": ["service mesh", "istio", "envoy", "sidecar", "linkerd"],
    "sidecar": ["sidecar", "边车", "proxy", "envoy", "istio"],
    "gitops": ["gitops", "声明式", "argocd", "fluxcd", "基础设施"],
    "发布": ["release", "deploy", "发布", "部署", "canary", "blue-green"],
    "mvcc": ["mvcc", "并发控制", "多版本", "multiversion", "隔离级别"],
    "并发控制": ["mvcc", "concurrency", "isolation", "锁", "lock"],
    "olap": ["olap", "分析型", "clickhouse", "列式", "数据仓库"],
    "oltp": ["oltp", "事务型", "在线事务", "transaction"],
    "tidb": ["tidb", "newsql", "分布式数据库", "pingcap"],
    "cockroachdb": ["cockroachdb", "newsql", "分布式", "sql"],
    "clickhouse": ["clickhouse", "列式", "分析", "olap", "分析型数据库"],
    "proxysql": ["proxysql", "读写分离", "中间件", "proxy"],
    "读写分离": ["read write split", "读写分离", "主从", "proxy", "proxysql"],
    "分库分表": ["sharding", "分库分表", "shardingsphere", "水平拆分"],
    "连接池": ["connection pool", "连接池", "druid", "hikari", "数据库"],
    "timescaledb": ["timescaledb", "时序", "time series", "postgresql"],
    "owasp": ["owasp", "安全风险", "web安全", "漏洞"],
    "vault": ["vault", "hashicorp", "密钥管理", "secret", "凭证"],
    "密钥管理": ["vault", "secret management", "密钥", "credential", "kms"],
    "pkce": ["pkce", "oauth", "移动端", "mobile", "授权"],
    "oidc": ["oidc", "openid connect", "身份认证", "auth"],
    "rbac": ["rbac", "abac", "权限", "role", "access control"],
    "零信任": ["zero trust", "零信任", "never trust", "always verify"],
    "trivy": ["trivy", "镜像扫描", "container security", "漏洞扫描"],
    "waf": ["waf", "web应用防火墙", "modsecurity", "应用防护"],
}


def tokenize(text: str) -> List[str]:
    """Tokenize text into ASCII words + CJK bigrams + CJK chars."""
    text = text.lower()
    ascii_tokens = re.findall(r'[a-z0-9_.+-]+', text)
    cjk_bigrams = _cjk_bigrams(text)
    cjk_chars = _cjk_char_tokens(text)
    return ascii_tokens + cjk_bigrams + cjk_chars


def tokenize_with_expansion(text: str) -> List[str]:
    """Tokenize and expand with synonyms."""
    base = tokenize(text)
    expanded = list(base)
    text_lower = text.lower()
    for key, synonyms in SYNONYM_MAP.items():
        if key in text_lower:
            expanded.extend(synonyms)
    return expanded
