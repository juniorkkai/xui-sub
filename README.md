# xui-sub

把 3x-ui 的订阅转换成 Clash / Mihomo 完整配置的小服务。

- **Clash / Mihomo UA**：返回完整 YAML —— 节点来自 3x-ui，DNS、策略组、规则来自本地模板。
- **其他 UA**（Shadowrocket、v2rayN 等）：原样返回 3x-ui 的 base64 节点列表。
- 不读 SQLite、不调面板 API、不缓存；每次请求实时拉上游。
- 复用 3x-ui 的 6 天 IP 证书，证书续期后自动热加载。
- 透传 `Subscription-Userinfo` / `Profile-Update-Interval` 等响应头。

## 工作方式

```
客户端 ──HTTPS 443──> xui-sub
                         ├─ Clash/Mihomo UA → /mihomo/<subId> → 取 proxies → Jinja 模板
                         └─ 其他 UA          → raw subPath      → 原样返回
                                            │
                                            └─ Host: <upstream.host>
```

**关键点**：3x-ui 用请求的 Host 头生成节点地址。服务向上游请求时发送
`Host: <upstream.host>`；如果 Host 是 `127.0.0.1`，3x-ui 会生成
`localhost:14784`，客户端就会连自己。配置里的 `upstream.host` 必须是客户端实际
访问的公网 IP 或域名。

## 目录

```
xui-sub/
├── app.py                     # 服务（Python 3.11+，stdlib + Jinja2 + PyYAML）
├── config.example.toml        # 配置模板
├── templates/clash.yaml.j2    # Clash/Mihomo 模板
├── systemd/xui-sub.service    # systemd unit
├── deploy.sh                  # 安装/更新脚本
├── tests/test_app.py          # 单元测试
├── requirements.txt           # 仅用于 venv/测试；systemd 用系统 Python 包
└── README.md
```

运行时文件：

| 路径 | 说明 |
| --- | --- |
| `/opt/xui-sub/app.py` | 服务代码 |
| `/etc/xui-sub/config.toml` | 配置（0600，含 raw subPath） |
| `/etc/xui-sub/templates/clash.yaml.j2` | 可编辑模板 |
| `/etc/systemd/system/xui-sub.service` | systemd unit |

## 安装 / 更新

```bash
apt install -y python3 python3-jinja2 python3-yaml
cd xui-sub
bash deploy.sh
```

首次运行会从 `config.example.toml` 生成 `/etc/xui-sub/config.toml`，然后退出。
编辑 `raw_path`、`upstream.host`、`branding.title` 后再次运行 `bash deploy.sh`。

脚本会安装文件、跑 `--check`、`systemctl enable/restart`。也可以手动：

```bash
install -m 600 config.example.toml /etc/xui-sub/config.toml
/usr/bin/python3 /opt/xui-sub/app.py -c /etc/xui-sub/config.toml --check
```

## 配置

| 字段 | 说明 |
| --- | --- |
| `server.listen` / `port` | 监听地址/端口，默认 443 |
| `server.cert` / `key` | 3x-ui 的 IP 证书路径 |
| `server.path_prefix` | 订阅前缀，默认 `/sub/` |
| `server.cert_check_interval` | 证书 mtime 检查间隔（秒） |
| `upstream.base` | 本机 3x-ui 订阅服务，默认 `https://127.0.0.1:3770` |
| `upstream.clash_path` | Clash 输出，默认 `/mihomo/{sub_id}` |
| `upstream.raw_path` | 面板订阅设置里的 **subPath**，例如 `/your-subpath/{sub_id}` |
| `upstream.host` | **客户端访问用的公网 IP/域名**，3x-ui 用它生成节点地址 |
| `upstream.verify_tls` | 回环请求默认 `false`（证书是公网 IP 的） |
| `templates.dir` / `clash` | 模板目录和文件名 |
| `branding.title` | `Profile-Title`，保持 ASCII |
| `security.allowed_tokens` | 空 = 接受任何 3x-ui 认可的 subId；填 subId 则只放行这些 |

`raw_path` 在面板改了 subPath 后要同步；Clash 用的 `/mihomo/` 是固定别名，不受影响。

改 `listen`/`port`/`cert`/`key`/`templates.dir` 需要 `systemctl restart xui-sub`；
其余配置保存后下一次请求自动生效。`systemctl reload xui-sub` 会重读配置和证书。

## 使用

```
https://<upstream.host>/sub/<subId>
```

`<subId>` 就是 3x-ui 客户端的 Sub ID。Clash/FLClash 直接填这个地址；
Shadowrocket 也填这个地址，会拿到 base64 节点列表。旧的 `:3770` 订阅不受影响。

## 加用户

在 3x-ui 里设置好 subId，直接用 `/sub/<subId>` 即可，不需要改配置或重启。
如果想锁定用户，把 subId 填进 `allowed_tokens`。

## 模板

编辑 `/etc/xui-sub/templates/clash.yaml.j2`，Jinja2 会自动重新加载。可用变量：

- `proxies`：节点 dict 列表，`p.name`、`p.type` 等；
- `usage`：`upload/download/total/expire/used/remaining/percent` 和 `*_h` 格式化值；
- `title`、`generated_at`、`update_interval`、`sub_id`。

节点行用 `{{ p | yaml_inline }}` 输出 YAML flow mapping；策略组里引用节点名用
`{{ p.name | jsonstr }}`。节点名沿用 3x-ui 的 remark，想改可在面板改备注模板。

改完先跑：

```bash
/usr/bin/python3 /opt/xui-sub/app.py -c /etc/xui-sub/config.toml --check
```

`--check` 会用假节点渲染模板并解析 YAML，同时检查证书和配置。

## 运维

```bash
systemctl status xui-sub
journalctl -u xui-sub -f
systemctl reload xui-sub
systemctl restart xui-sub
```

### 证书

复用 `/root/cert/ip/fullchain.pem` + `privkey.pem`。服务每
`cert_check_interval` 秒检查 mtime，续期后自动加载；`reload` 也可手动触发。
证书到期前可以在日志里看到 `TLS certificate loaded`。

### 排障

| 现象 | 排查 |
| --- | --- |
| 404 | subId 不存在/客户端被禁用/无可用节点；对照 `https://127.0.0.1:3770/mihomo/<subId>` |
| 502 | 3x-ui 订阅不可达、超时、返回非 200 或坏 YAML；看 `journalctl -u xui-sub` |
| 500 | 模板语法错误；跑 `--check` |
| Clash 404/502 但 raw 正常 | 面板订阅设置里 Clash/Mihomo 输出（`subClashEnable`）是否开启 |
| 节点地址是 localhost | `upstream.host` 为空或为回环地址；填客户端能访问的公网 IP/域名 |
| 客户端不认 GEOSITE | FLClash 首次使用会下载 Geo 数据；仍失败就把模板里的 GEOSITE 换成 DOMAIN-SUFFIX |
| 证书错误 | `openssl s_client -connect 127.0.0.1:443 -servername <host>`，确认读的是 `/root/cert/ip/` |
| 端口占用 | `ss -tlnp \| grep :443`；443 不能再给 Reality 用 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

需要 `Jinja2` 和 `PyYAML`；系统包或 `pip install -r requirements.txt` 均可。
测试不访问网络、不需要证书。

## 安全

- 订阅 URL 里的 subId 就是密码，使用 3x-ui 生成的随机值。
- 只允许 GET/HEAD；返回 `Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
- 日志只记录 token 前 4 位，不记录完整 subId、节点 UUID 或密钥。
- 需要收紧时把 subId 填进 `allowed_tokens`。

## 回滚

```bash
systemctl disable --now xui-sub
rm -f /etc/systemd/system/xui-sub.service
systemctl daemon-reload
```

443 随即释放；3x-ui、Reality、3770 原订阅均未改动。
