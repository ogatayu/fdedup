# fdedup

Fdedup は Windows 向けの重複ファイル確認・削除ツールです。2つのディレクトリを指定し、サブフォルダを含むファイルから厳密一致または類似候補を検出します。

## 主な機能

- SHA-256 による厳密一致の重複検出
- Pillow が入っている場合の厳格な画像内容一致・類似検出
- ffmpeg / ffprobe が インストールされている場合は、厳格に動画サンプル一致を検出
- UNC パスに対応
- フルパスに特定の単語を含むファイルを優先して残すようにする
- TUI上での候補確認、KEEP/DELETE切り替え、一括選択
- ごみ箱への移動、または完全削除の選択
- 全ファイルを削除してしまうグループを削除前にブロック

## 実行方法

### venvを使用した仮想環境の構築（任意） 

作業ディレクトリで Python の仮想環境を作成します。

```powershell
python -m venv .venv
```

仮想環境を有効化します。

```powershell
.\.venv\Scripts\Activate.ps1
```

依存パッケージをインストールします。

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

作業を終了する場合は、仮想環境を無効化します。

```powershell
deactivate
```

### プログラムの実行

```powershell
python -m pip install -r requirements.txt
python -m fdedup "C:\path\to\DirectoryA" "D:\path\to\DirectoryB"
```

ディレクトリ引数を省略した場合は、起動時に `Directory A` と `Directory B` の入力を求められます。
同じディレクトリを2回指定すると、そのディレクトリ内で重複を探します。

## TUIの操作

- `Up` / `Down` / `PgUp` / `PgDn`: 行を移動
- `Home` / `End`: 先頭または末尾へ移動
- `Space` / `Enter`: 現在行を `KEEP` / `DELETE` で切り替え
- `r`: 全グループに推奨選択を適用
- `g`: 現在のグループに推奨選択を適用
- `a`: Directory A 側を優先して残す
- `b`: Directory B 側を優先して残す
- `o`: 並び替え項目を切り替え
- `O`: 並び替え方向を反転
- `d`: `DELETE` にしたファイルを削除確認へ進める
- `?`: ヘルプと警告を表示
- `q` / `Esc`: 終了

削除は確認キーを押すまで実行されません。また、各重複グループで少なくとも1件は残るようにチェックします。

## コマンドラインオプション

```powershell
python -m fdedup [DirectoryA] [DirectoryB] [options]
```

- `--marker TEXT`: 優先して残すフルパスマーカーを指定します。複数回指定できます。省略時は `★` です。
- `--no-image-similarity`: 画像類似検出を無効化します。
- `--no-video-similarity`: 動画類似検出を無効化します。
- `--permanent`: ごみ箱へ移動せず完全削除します。

## 類似判定の仕様

通常のファイルは SHA-256 によるバイト完全一致だけを重複として扱います。

画像は Pillow が使える場合だけ追加判定します。EXIF補正後にRGBピクセル列が一致する画像は、PNG/BMPなど形式が違っていても `image-content` として扱います。ピクセル完全一致ではない場合は、幅・高さが同じで、dHash距離が4以下、かつ32x32 RGBサムネイルの平均差分が6.0以下の場合だけ `image` 類似として扱います。

動画は ffmpeg / ffprobe が使える場合だけ追加判定します。長さ・幅・高さが取得できない動画は類似判定から除外します。幅・高さが同じで、長さ差が0.5秒以内または1%以内の動画について、10%, 30%, 50%, 70%, 90% の5フレームを比較します。4フレーム以上が近く、合計距離も閾値内の場合だけ `video` 類似として扱います。

## 並列処理

ハッシュ計算、画像fingerprint生成、動画fingerprint生成は並列処理します。並列数は起動時のPCスペックから自動決定します。現在の既定ロジックでは、ハッシュ/画像は論理CPU数の半分を目安に最大8、動画はffmpegプロセス負荷を考慮して論理CPU数の4分の1を目安に最大4です。物理メモリが8GB未満の場合はさらに抑制します。

スキャン中に `Parallel workers: hash=..., image=..., video=...` と表示されます。fingerprint生成は並列化しますが、重複グループへの結合はメインスレッドで順序付きに行うため、並列数によって結果が変わらない設計です。

## ffmpeg / ffprobe

動画類似検出には `ffmpeg.exe` と `ffprobe.exe` が必要です。
例として、 `winget` を使用したインストール方法を以下に示します。

```powershell
winget install -e --id Gyan.FFmpeg
```

インストール後、ターミナルを開き直して確認します。

```powershell
ffmpeg -version
ffprobe -version
where.exe ffmpeg
where.exe ffprobe
```

見つからない場合は、ffmpeg の `bin` フォルダをユーザー環境変数 `Path` に追加してください。
