# Daily MVI — 決定的生成Primary・LLMは後続の実験

Tracking: Issue [#444](https://github.com/upiscium/ObsidianAutomation/issues/444), existing Issue/PR output contract [#324](https://github.com/upiscium/ObsidianAutomation/issues/324), parent Draft [#391](https://github.com/upiscium/ObsidianAutomation/pull/391).

## 目的

「昨日何やったっけ？」を日常的に**人手の要約承認なし**で確認できるようにする。Primary生成にはLLM/GPU/モデルプロファイルを使用しない。LLMによる自然文短縮・意味的抽出は、決定的な生成と表示が完成した後の**任意の実験**とする。

This source-only first increment is a **fully executable deterministic CAS-to-Markdown preview** (including a safe offline Daily section preview), not an applied production Writer/Timer release.

## 入力と生成アルゴリズム

1. 既存Collectorのtrusted Capture Index SHA-256を読み込み、**C1a/C0が検証した元GitHub Evidence**へ再接続。CASのoriginal-only項目も含めてevent censusを検証し、未帰属default-branch commitは元Evidenceに残すが勝手にIssue/PRへ割り当てない。
2. Typed Eventから正確な `repository + issue/pull_request + number` を構築。GitHubリンクは検証済み主体からのみ組み立て、モデル/コメント中のURLを表示先に使わない。
3. `pull_request_merged`、Close/Reopen、作成、レビュー、実装/修正/テストcommit、実質コメントを有限のルールで分類。同じIssue/PRの更新は**1項目**へ統合。Mergeは型付きイベントからのみ確定し、古いDraftコメントより優先する。
4. 日本語の定型句を1〜3文で描画。長文をAIで翻訳・圧縮しない。コメント中の実GPU・PASS・承認等は**「報告があった」**と表記し、実サーバ観測・正式GA移行の確認と区別する。タイトルが当日のmutable sourceとして利用できない場合は元コミットのfirst-lineを引用する。
5. Format-only、bot、`Next`等のnoiseは原文を消さず表示から除外。要約できない本文を勝手に補完しない。コミット時刻からPush時刻を推測せず、実元Evidenceが不正ならFail Closed。
6. 空の日は一行の「進展のあったIssue・PRはありません。」だけ。通常のユーザー向け出力はGitHubリンクを備えたRepo別Issue/PR項目であり、脇のdiagnosticに手作業のレビュー要求を出さない。

### 経路

```text
verified GitHub source-route / original CAS
   ↓ source census / original Issue-PR identity / JST date
deterministic_digest.build_digest_from_verified_capture
   ↓  typed change classification; zero model inference
Japanese Issue/PR Markdown + immutable private receipt
   ↓  optional read-only preview with existing section-only CAS Writer logic
Private Daily preview (no WebDAV PUT, no Timer)
```

模型を用いる従来のC1b/C2a/C5比較はこのPrimaryに**一切不要**です。あとからLLMを使うなら、決定的出力を保存・保持したまま別の候補として比較し、失敗やGrounding不受理で元出力を失わせないようにします。

## 利用方法（開発用・非本番）

前日分のCapture Index SHAが既存のtrusted Collectorによって提供され、正当なCAS読取UID/GIDで実行できる場合:

```bash
nix develop --command python -m obsidian_automation.github_daily_deterministic_preview \
  --capture-sha <64桁のCapture Index SHA> \
  --yesterday
```

より明示的な固定日プレビュー（実験）:

```bash
nix develop --command python -m obsidian_automation.github_daily_deterministic_preview \
  --capture-sha <64桁のCapture Index SHA> \
  --target-date 2026-10-18 \
  --output-dir <private-existing-output-directory> \
  --preview-daily <existing-local-daily-note.md>
```

`--output-dir`は作成済みの非公開ディレクトリを指定。Markdownとモデル非使用を明記したsource-ref/receipt JSONを**書換不可のcreate-only**で保存します。`--preview-daily`はローカルのDaily Noteを読み、既存 `prepare_daily_update` により `# Project Progress` 区間だけを変更した**新しいpreviewファイル**を出力します。元Noteへ書かず、NextcloudのWebDAV PUTは呼び出しません。

本番スケジューラーは、対象日のCollector CAS SHAを自動的に選択する段階と、別途許可されたWriterへ新しいversioned deterministic artifactを安全に転送する段階が必要です。現行の旧LLM型 `grounded_summary_sha256` を偽造してWriterに流用してはなりません。正式なstage/receiptの受入・rollout gateを検証するまで、本機能は**プレビューとして完成**、**運用リリースは未完了**です。

## 実GitHub証拠での試験

Adamの既存private ResearchEvidenceにある **2026-10-18 JSTのGitHub source** (upiscium/PROJECT-SUZKA / TSUMGI, **71 event / 15 Issue-PR / 2 repository**) を固定してモデル呼出なしで生成。typed mergeにより **TSUMGI PR #155** は当日マージと表示、先行Draft/no-merge文を当日末の状態として宣言しない。**SUZKA PR #330** はtest-only code changeを表示し、本番稼働/製品機能完成を創作しない。 Issue #151は実GPUに関する**コメント内報告**として提示し、実ホストの最終cutoverを証明したとは書かない。

private preview artifacts (actual source text is NOT published on GitHub):

`~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-Acceptance-20261020/deterministic-no-llm-2026-10-18/`

`--preview-daily`で既存 `# My Notes` をバイト単位で保持し、`# Project Progress`のみ置換する事前確認ができます。ユーザーからの毎日の品質承認・データラベリングは不要です。

## 残る課題と非目標

- 定型文は意図的に保守的で、長文中の意思決定や細部の重要性を完全に復元しません。正確なGitHubリンクで原文に移動できることを優先します。LLMに誤訳・抜け落ち・時系列誤認を任せません。
- GitHubのコメント中に「PASS」「承認」「GPU」が記載されても、型付きAPI状態とライブ稼働の実測値は別物です。「報告があった」と記載します。
- Titleが歴史的に不可視なら元commit subjectを**文字列のまま**提示し、誤訳しません。取得不能な過去のタイトルを想像で作りません。
- 初回はread-only preview。**定期的なCapture選択→Daily Writerのstaging integration→本番適用・Timer有効化**は別途実装/試験/承認が必要です。古いETag/If-MatchやWriter権限を緩めないでください。
- 人手評価を日常的な出力ゲートには戻さない。Issue #386の品質サンプルや [Draft #432](https://github.com/upiscium/ObsidianAutomation/pull/432) の独立ラベル評価は開発時の研究・受入みに限定。
- [Draft #426](https://github.com/upiscium/ObsidianAutomation/pull/426) のBaseline C5プロンプト変更は、MVIの必須条件から外れています。これを無承認で合流しないこと。
- Source-code PRはsource-onlyであり、本番LXC、Nextcloud Writer、Vault、Scheduler、モデル/TSUMGI serviceの設定を変更しない。

## 独立したプライベート実験環境に関する注意

初回実データの検証には、Adam上の既存ResearchEvidenceに隔離したCASと
テスト専用のCollector/reader ACL fixtureを使用しました。正しい原本
DigestとC1a/C0のsource censusは再照合していますが、**稼働中LXCの
実Collector UID/GID・実Writer/Timerを通した本番受け入れではありません**。
後続の本番ゲートはこの隔離試験と区別して実施してください。
