# EasyWebCrawl

一个面向分享的公开版爬虫示例仓库。仓库保留 5 个最终 Python 脚本，按平台整理：

- `examples/99designs/requests_webstructure_99designs_contest.py`
- `examples/ssrn/selenium_crawl4ai_webstructure_ssrn_paper.py`
- `examples/reddit/praw_api_reddit_submission_enrich.py`
- `examples/tiktok/playwright_webstructure_tiktok_creator_marketplace.py`
- `examples/tiktok/playwright_api_tiktok_capture.py`

## 仓库特点

- `99designs` 三个旧脚本已经合并成一个公开版脚本，支持 `list`、`brief`、`entries`、`all` 四种模式。
- `SSRN` 两个旧 notebook 已经合并成一个公开版脚本，支持 `list`、`detail`、`all` 三种模式。
- 所有公开入口都改成了 `.py`。
- 仓库已移除旧版脚本、notebook、Node 抓包文件。
- 不再保留真实 cookie、token、绝对路径、私有数据库和邮箱验证码逻辑。

## 安装说明

依赖按方法分组安装，不要求一次性全部安装。

```bash
pip install requests beautifulsoup4
pip install selenium webdriver-manager
pip install crawl4ai
pip install praw
pip install playwright
playwright install chromium
```

## 运行示例

### 1. 99designs

比赛列表抓取：

```bash
python3 examples/99designs/requests_webstructure_99designs_contest.py \
  --mode list \
  --url "https://99designs.hk/logo-design/contests?sort=start-date%3Adesc&status=won" \
  --output output/99designs
```

列表、brief、entries 串联执行：

```bash
python3 examples/99designs/requests_webstructure_99designs_contest.py \
  --mode all \
  --url "https://99designs.hk/logo-design/contests?sort=start-date%3Adesc&status=won" \
  --output output/99designs
```

页面需要登录态时，可通过 JSON 文件传入请求头和 cookie：

参考该网站：https://blog.csdn.net/qingliuun/article/details/131168368

```json
{
  "User-Agent": "Mozilla/5.0 ..."
}
```

```json
{
  "session_cookie_name": "your_cookie_value"
}
```

### 2. SSRN

分类列表抓取：

```bash
python3 examples/ssrn/selenium_crawl4ai_webstructure_ssrn_paper.py \
  --mode list \
  --input data/ssrn_category_list.csv \
  --output output/ssrn \
  --headless
```

论文详情和作者信息抓取：

```bash
python3 examples/ssrn/selenium_crawl4ai_webstructure_ssrn_paper.py \
  --mode detail \
  --input output/ssrn/paper_list.csv \
  --output output/ssrn
```

完整链路执行：

```bash
python3 examples/ssrn/selenium_crawl4ai_webstructure_ssrn_paper.py \
  --mode all \
  --input data/ssrn_category_list.csv \
  --output output/ssrn \
  --headless
```

### 3. Reddit

运行前配置环境变量，示例见 `.env.example`。

```bash
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
export REDDIT_USER_AGENT="easywebcrawl-demo"
python3 examples/reddit/praw_api_reddit_submission_enrich.py \
  --input data/reddit_submission_ids.csv \
  --output output/reddit/reddit_submission_enrich.csv
```

### 4. TikTok

页面结构抓取示例：

```bash
python3 examples/tiktok/playwright_webstructure_tiktok_creator_marketplace.py \
  --url "https://seller-us.tiktok.com/creator-marketplace" \
  --output output/tiktok/creator_marketplace.csv
```

接口返回抓包示例：

```bash
python3 examples/tiktok/playwright_api_tiktok_capture.py \
  --target-url "https://seller-us.tiktok.com/creator-marketplace" \
  --url-includes "/api/creator" \
  --output output/tiktok/captured_api_responses.json
```

## 输出说明

- `output/99designs/contest_list.csv`
- `output/99designs/contest_brief.csv`
- `output/99designs/contest_entries.csv`
- `output/ssrn/paper_list.csv`
- `output/ssrn/paper_detail.csv`
- `output/ssrn/author_info.json`
- `output/reddit/reddit_submission_enrich.csv`
- `output/tiktok/creator_marketplace.csv`
- `output/tiktok/captured_api_responses.json`

## 方法文档

更详细的中文介绍见：

- [docs/crawler_methods_zh.md](docs/crawler_methods_zh.md)
