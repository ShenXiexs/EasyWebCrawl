# 爬虫方法

这个仓库保留 5 个公开版脚本，用于展示不同平台下常见的抓取方式，原始业务耦合代码已移除。

## 1. requests + BeautifulSoup

对应脚本：

- `examples/99designs/requests_webstructure_99designs_contest.py`

适合场景：

- 页面结构相对稳定
- 主要目标是 HTML 中的列表、详情、标签、统计字段
- 不依赖复杂前端交互

脚本结构：

- `list` 模式抓比赛列表
- `brief` 模式抓比赛 brief 信息
- `entries` 模式抓 entry、设计师资料和图片链接
- `all` 模式按顺序串联

公开版和旧版的区别：

- 去掉了硬编码 cookie 和 WAF token
- 去掉了绝对路径
- 改为通过参数和 JSON 配置传入请求头、cookie
- 统一输出到 `output/99designs/`

## 2. Selenium

对应脚本：

- `examples/ssrn/selenium_crawl4ai_webstructure_ssrn_paper.py`

其中 `list` 阶段使用 Selenium。

适合场景：

- 页面依赖浏览器执行
- 列表在浏览器渲染后才完整出现
- 需要模拟点击、等待和翻页

`list` 模式内容：

- 读取 SSRN 分类列表 CSV
- 打开分类页面
- 遍历分页
- 提取论文标题、发布时间、论文链接

## 3. crawl4ai

对应脚本：

- `examples/ssrn/selenium_crawl4ai_webstructure_ssrn_paper.py`

其中 `detail` 阶段优先使用 `crawl4ai`，失败时再回退到 `requests`。

适合场景：

- 详情页结构复杂
- 希望统一使用异步抓取器获取页面 HTML
- 需要进一步进入作者页补全资料

`detail` 模式内容：

- 读取 `paper_list.csv`
- 抓论文详情页
- 提取摘要、关键词、下载量、引用量等字段
- 进入作者页补充作者机构、论文数、总引用数
- 输出 `paper_detail.csv` 和 `author_info.json`

## 4. PRAW / 官方 API

对应脚本：

- `examples/reddit/praw_api_reddit_submission_enrich.py`

适合场景：

- 平台本身提供稳定 API
- 你已有对象 ID，只需要补充元数据

处理流程：

- 从输入 CSV 读取 submission id
- 调用 Reddit API 拉取 title、subreddit、score、comments 等字段
- 输出新的 CSV

该方式依赖 API 凭证配置。

## 5. Playwright 页面结构抓取

对应脚本：

- `examples/tiktok/playwright_webstructure_tiktok_creator_marketplace.py`

适合场景：

- 页面交互复杂
- 需要滚动加载
- 页面依赖登录态

公开版保留的功能：

- 支持传入 `storage state`
- 支持搜索关键词
- 支持滚动加载更多卡片
- 抽取当前页面可见创作者卡片的基础字段

旧版里那些私有逻辑已经移除：

- 数据库写入
- 邮箱验证码
- 账号体系
- 任务调度
- 私有模块导入

## 6. Playwright + CDP 抓接口返回

对应脚本：

- `examples/tiktok/playwright_api_tiktok_capture.py`

适合场景：

- 页面数据由前端接口返回
- 需要抓真实 API 响应，而不是只抓 DOM
- 需要判断分页接口里的 `has_more`

处理流程：

- 打开目标页面
- 监听浏览器网络请求
- 过滤 URL 包含指定关键词的接口
- 抓取 JSON 响应体
- 输出为完整 JSON 文件

## 方法关系

1. `requests + BeautifulSoup` 对应基础 HTML 抓取。
2. `Selenium` 对应浏览器驱动的列表抓取。
3. `crawl4ai` 对应详情页与作者页的补充抓取。
4. `PRAW API` 对应官方接口数据补全。
5. `Playwright` 对应页面自动化和网络接口捕获。
