# A股恐慌指数：独立运行版

下载代码后，在自己电脑上获取历史行情并计算市场压力，无需原作者的量化仓库、行情数据包、Hikyuu数据库或券商账户。

保留短期五因子恐慌、长期损伤、指数过热度、QVIX/备用IV30和离线交互看板。代码不下单，不自动调度。

## 安装

Python 3.10+。在本仓库根目录执行：

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 使用：.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install .
```

依赖为 NumPy、pandas、requests、Plotly、BaoStock 和 tzdata。开发测试使用 `python -m pip install -e '.[dev]'`。

安装后也可以用 `ashare-panic` 代替 `python -m trade.qvix`。全部运行数据默认写入**当前工作目录的 `data/`**，建议一直在仓库根目录运行；也可以始终指定同一个绝对路径 `--data-dir`。

## 第一次使用

```bash
python -m trade.qvix init-data
python -m trade.qvix report --open
```

`init-data` 会：

1. 获取沪深A股证券列表和实际交易日历，纳入下载区间内源可发现的退市股票。
2. 逐股下载2009年以来不复权日线和复权因子，校验日期、价格、成交额及交易日缺口。
3. 下载指数和可获得的QVIX历史，建立本地SQLite指标库，计算短期/长期评分并生成 `data/report.html`。

首次需要下载数千只股票，耗时取决于源速度，可能较长。进度输出到终端；Ctrl+C中断后重跑同一命令即可续传，已校验完成的股票不会重复下载。请保持单进程运行。

可以缩短初始历史，例如 `init-data --start 2020-01-01`；需要为指标预留足够预热时间。短期分位至少252个有效先前样本、默认窗口756个交易日，长期损伤需要约320日输入；数据过短时保留缺失，不输出伪造分数。不同起始日、数据源、股票池或历史修订可能使分数不同。

## 每日使用

```bash
# 建议北京时间18:30以后：补股票日线、复权因子和指标
python -m trade.qvix update

# 交易时段：只更新恐慌和长期损伤，速度更快
python -m trade.qvix snapshot --panic-only

# 交易时段：同时尝试获取QVIX/期权IV30
python -m trade.qvix snapshot

# 打开看板
python -m trade.qvix report --open
```

HTML本身不会抓行情，命令执行后刷新浏览器即可。`update` 在18:30前保守地只请求之前日期的数据；源实际尚未发布时会报缺数，稍后重跑。盘中运行 `update` 不会把形成中的日线存成历史。

日常下载回查最近20个自然日，复权因子每次新交易日下载时重新获取完整事件表；发现历史价格或因子修订时，从最早受影响日期重算。重算起点也会跨中断保留。

```bash
# 强制检查/补录更早历史，保留已经下载的更早记录
python -m trade.qvix update --start 2024-01-01

# 数据位置可修改，全局参数放在子命令之前
python -m trade.qvix --data-dir /your/local/panic-data update
```

## 数据源与复权

| 输入 | 获取方式 |
|---|---|
| 股票列表、交易日历、个股日线、复权因子 | BaoStock匿名接口，无需自备账号密钥 |
| 六类指数日线、股票盘中报价 | 腾讯公开接口 |
| QVIX历史/分时 | OptBBS；失败时明确显示缺失 |
| 备用IV30 | 新浪带日期的标准期权买卖报价 |
| RPS | 可选用户自备CSV，默认不下载和计算RPS排名 |

股票历史使用 `adjustflag=3` 的原始价格，按日期向后匹配 `backAdjustFactor`；生效前不使用未来因子。复权后的价格只用于统计指标。盘中用已复权历史尾价 × 当前价 / 当天报价的昨收参考构造因果价格；不会重复叠加除权。

原程序用Hikyuu权息事件计算复权；独立版改用BaoStock因子，算法参数沿用，数据口径存在差异，不能承诺与原看板逐日数值一致。数据源可能修订历史，退市历史完整性未经独立验证。

源返回完整暂停交易行时保留该行，成交额按0处理，不计入当日交易股票；若请求区间出现未解释的交易日缺口则记为失败，不填造行情。股票输入有下载失败时，`init-data`/`update` 保留成功文件并停止进入新一轮指标计算，避免只下载一部分却宣称覆盖全市场。

QVIX是独立辅助指标，不参与短期五因子得分。其源不可用时，股票评分仍可有效；备用IV30是独立估计，跨口径参考分位未校准，同方法历史不足则留空。

源文档：[证券基本资料](https://www.baostock.com/mainContent?file=stockBasic.md)、[本地价格与复权因子](https://www.baostock.com/mainContent?file=localdatafactorInfo.md)、[BaoStock API](https://github.com/zxygithub/baostock/blob/master/docs/pythonAPI.md)。接口和数据使用需遵守各自来源条款；项目不附送第三方行情数据或授予行情再分发许可。

## 文件与Git边界

```text
trade/qvix/           算法、下载器、CLI、看板资源、测试
scripts/             小样本数据源检查与离线验收
data/                自动生成，Git忽略
  market/
    daily/           个股不复权日线与后复权因子
    factors/         原始因子事件表
    universe.csv     本次发现的证券列表
    calendar.csv     下载用交易日历
    checkpoint.json  逐股续传、文件校验和重算起点
    download-latest.json
  qvix.sqlite3       历史指标、修订与采样日志
  raw/               行情响应及来源元数据
  cache/             可重建的计算缓存
  closes/            独立盘后报价补充
  report.html        可直接打开的交互看板
```

Git中只提交源码、文档、测试和看板模板。`data/`、虚拟环境、缓存、日志、密钥文件已忽略。没有打包原作者的数据；不要用 `git add -f data`。

## 常见问题

- **网络失败**：BaoStock使用独立TCP连接，普通HTTP代理不一定覆盖它。先运行下方小样本命令；源不可达时换可用网络或稍后重试。不要把旧数据日期当成今天。
- **退出码2**：部分数据不可用。查看JSON的 `errors`；仅QVIX失败不代表股票评分失败，`panic`/`damage`失败则对应评分不可用。退出码1为参数或运行错误，130为手动中断。
- **中断后的写入锁**：正常Ctrl+C会释放锁。被强制杀进程或断电可能留下 `data/.writer.lock`，确认没有更新进程后手动移除该文件再重跑。
- **初始化后只想重算**：`sync` 读取已有股票文件并更新指数/指标；股票下载用 `update`。`report` 只重建展示。
- **RPS空白**：属于默认行为，不影响市场压力。可通过 `--rps-counts` 接入已有同口径汇总CSV；高级 `report --refresh-rps` 只汇总自备排名历史，不提供排名下载。

## 验证

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
node --test trade/qvix/tests/test_cursor.cjs  # 可选，仅测试前端交互

# 少量真实股票下载；使用单独目录，不会生成全市场评分
python -m trade.qvix --data-dir data/smoke download --symbols 600000.SH 688001.SH 300308.SZ --start 2025-01-01 --end 2025-12-31

# 不联网，使用明确标记的1000只合成股票验收完整初始化/更新/看板
python -m scripts.check_pipeline
```

小样本成功不能证明全部股票、全部日期都已验证。详见 `VALIDATION.md` 的实际验收范围。

短期恐慌分数描述相对历史压力，长期损伤描述广泛回撤状态，均不是触底概率或已经验证的买卖信号。
