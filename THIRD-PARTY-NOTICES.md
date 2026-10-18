# QIO 第三方开源组件声明

本文件由 `scripts/licenses/build_third_party.py` 生成，**请勿手工编辑**；
覆盖率由 `scripts/licenses/check_third_party.py` 校验：锁文件里少一个组件就失败。

覆盖范围：三套锁文件里的全部组件（含只在其它平台用到的），外加随包分发的资产。
许可证全文见文末；正文里的版权行缺失时会在该条目上写明出处。

生成输入（sha256 前 16 位）：
- `backend/uv.lock` — `d81e54eb68744d6d`
- `frontend/package-lock.json` — `b261ff84391b481a`
- `frontend/src-tauri/Cargo.lock` — `c5d4b586ce709fe6`

## 组件与许可证

### (Apache-2.0 OR MIT) AND BSD-3-Clause（1 个）

- `encoding_rs@0.8.35`（rust） — Copyright Mozilla Foundation

### (MIT OR Apache-2.0) AND Unicode-3.0（1 个）

- `unicode-ident@1.0.24`（rust） — Copyright © 1991-2023 Unicode, Inc.

### 0BSD OR MIT OR Apache-2.0（1 个）

- `adler2@2.0.1`（rust） — 版权行见上游：https://github.com/oyvindln/adler2

### Apache-2.0（15 个）

- `@dimforge/rapier3d-compat@0.12.0`（npm） — Copyright 2020 Dimforge EURL
- `expect-type@1.4.0`（npm） — Copyright 2024 Misha Kaletsky
- `pdfjs-dist@6.4.299`（npm） — 版权行见上游：https://github.com/mozilla/pdf.js
- `typescript@5.9.3`（npm） — 版权行见上游：Microsoft Corp. / https://github.com/microsoft/TypeScript
- `xml-name-validator@5.0.0`（npm） — 版权行见上游：Domenic Denicola <d@domenic.me> (https://domenic.me/) / jsdom/xml-name-validator
- `flatbuffers@25.12.19`（python） — 版权行见上游：Derek Bailey
- `hf-xet@1.6.0`（python） — 版权行见上游：https://huggingface.co/docs/hub/xet/index
- `huggingface-hub@1.31.0`（python） — 版权行见上游：Hugging Face, Inc.
- `importlib-metadata@9.0.1`（python） — 版权行见上游：Jason R. Coombs <jaraco@jaraco.com> / https://github.com/python/importlib_metadata
- `openai@3.13.0`（python） — Copyright 2026 OpenAI
- `pytest-asyncio@1.4.0`（python） — 版权行见上游：Tin Tvrtković <tinchester@gmail.com> / https://github.com/pytest-dev/pytest-asyncio/issues
- `requests@2.34.2`（python） — Copyright 2019 Kenneth Reitz
- `tokenizers@0.23.2`（python） — 版权行见上游：Nicolas Patry <patry.nicolas@protonmail.com>, Anthony Moi <anthony@huggingface.co> / https://github.com/huggingface/tokenizers
- `sync_wrapper@1.0.2`（rust） — 版权行见上游：https://github.com/Actyx/sync_wrapper
- `tao@0.35.3`（rust） — 版权行见上游：https://github.com/tauri-apps/tao

### Apache-2.0 / MIT（1 个）

- `fnv@1.0.7`（rust） — Copyright (c) 2017 Contributors

### Apache-2.0 AND CNRI-Python（1 个）

- `regex@2026.9.10`（python） — Copyright 2020 Matthew Barnett

### Apache-2.0 AND ISC（1 个）

- `ring@0.17.14`（rust） — Copyright (c) 2009 The Go Authors. All rights reserved.；Copyright 2015 The Chromium Authors. All rights reserved.

### Apache-2.0 AND MIT（1 个）

- `dpi@0.1.2`（rust） — Copyright (c) 2018 Jorge Aparicio；Copyright © 2005-2020 Rich Felker, et al.

### Apache-2.0 OR BSD-2-Clause（1 个）

- `packaging@26.3`（python） — 版权行见上游：Donald Stufft <donald@stufft.io> / https://packaging.pypa.io/

### Apache-2.0 OR BSD-3-Clause（1 个）

- `cryptography@50.0.1`（python） — 版权行见上游：https://pypi.org/project/cryptography/

### Apache-2.0 OR ISC OR MIT（3 个）

- `hyper-rustls@0.27.10`（rust） — Copyright (c) 2016, Joseph Birr-Pixton <jpixton@gmail.com>；Copyright (c) 2016 Joseph Birr-Pixton <jpixton@gmail.com>
- `rustls@0.23.45`（rust） — Copyright (c) 2016, Joseph Birr-Pixton <jpixton@gmail.com>；Copyright (c) 2016 Joseph Birr-Pixton <jpixton@gmail.com>
- `rustls-native-certs@0.8.4`（rust） — Copyright (c) 2016, Joseph Birr-Pixton <jpixton@gmail.com>；Copyright (c) 2016 Joseph Birr-Pixton <jpixton@gmail.com>

### Apache-2.0 OR MIT（51 个）

- `@tauri-apps/api@2.11.1`（npm） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `@tauri-apps/cli@2.11.4`（npm） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `@tauri-apps/cli-darwin-arm64@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-darwin-arm64
- `@tauri-apps/cli-darwin-x64@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-darwin-x64
- `@tauri-apps/cli-linux-arm-gnueabihf@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-linux-arm-gnueabihf
- `@tauri-apps/cli-linux-arm64-gnu@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-linux-arm64-gnu
- `@tauri-apps/cli-linux-arm64-musl@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-linux-arm64-musl
- `@tauri-apps/cli-linux-riscv64-gnu@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-linux-riscv64-gnu
- `@tauri-apps/cli-linux-x64-gnu@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-linux-x64-gnu
- `@tauri-apps/cli-linux-x64-musl@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-linux-x64-musl
- `@tauri-apps/cli-win32-arm64-msvc@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-win32-arm64-msvc
- `@tauri-apps/cli-win32-ia32-msvc@2.11.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@tauri-apps/cli-win32-ia32-msvc
- `@tauri-apps/cli-win32-x64-msvc@2.11.4`（npm） — 版权行见上游：https://github.com/tauri-apps/tauri
- `atomic-waker@1.1.2`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `autocfg@1.5.1`（rust） — Copyright (c) 2018 Josh Stone
- `bit-set@0.8.0`（rust） — Copyright (c) 2023 The Rust Project Developers
- `bit-vec@0.8.0`（rust） — Copyright (c) 2023 The Rust Project Developers
- `cargo_toml@0.22.3`（rust） — 版权行见上游：https://gitlab.com/lib.rs/cargo_toml
- `cesu8@1.1.0`（rust） — 版权行见上游：https://github.com/emk/cesu8-rs
- `ctor@0.8.0`（rust） — 版权行见上游：https://github.com/mmastrac/rust-ctor
- `ctor-proc-macro@0.0.7`（rust） — 版权行见上游：https://github.com/mmastrac/rust-ctor
- `dtor@0.3.0`（rust） — 版权行见上游：https://github.com/mmastrac/rust-ctor
- `dtor-proc-macro@0.0.6`（rust） — 版权行见上游：https://github.com/mmastrac/rust-ctor
- `equivalent@1.0.2`（rust） — Copyright (c) 2016--2023
- `fastrand@2.5.0`（rust） — 版权行见上游：https://github.com/smol-rs/fastrand
- `idna_adapter@1.2.2`（rust） — 版权行见上游：https://github.com/hsivonen/idna_adapter
- `indexmap@1.9.3`（rust） — Copyright (c) 2016--2017
- `indexmap@2.14.0`（rust） — Copyright (c) 2016--2017
- `libappindicator@0.9.0`（rust） — Copyright (c) 2017-2021 qDot；Copyright (c) 2021 Tauri Apps Contributors
- `libappindicator-sys@0.9.0`（rust） — 版权行见上游：https://crates.io/crates/libappindicator-sys
- `muda@0.19.3`（rust） — 版权行见上游：https://github.com/tauri-apps/muda
- `pin-project-lite@0.2.17`（rust） — 版权行见上游：https://github.com/taiki-e/pin-project-lite
- `rustc-hash@2.1.3`（rust） — 版权行见上游：https://github.com/rust-lang/rustc-hash
- `simd_cesu8@1.2.0`（rust） — 版权行见上游：https://github.com/seancroach/simd_cesu8
- `tauri@2.11.5`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-build@2.6.3`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-codegen@2.6.3`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-macros@2.6.3`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-plugin@2.6.3`（rust） — 版权行见上游：https://github.com/tauri-apps/tauri
- `tauri-plugin-log@2.9.2`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-plugin-process@2.3.1`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-plugin-shell@2.3.5`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-plugin-updater@2.12.0`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-runtime@2.11.3`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-runtime-wry@2.11.4`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `tauri-utils@2.9.3`（rust） — Copyright (c) 2017 - Present Tauri Apps Contributors
- `utf8_iter@1.0.4`（rust） — Copyright Mozilla Foundation
- `uuid@1.24.0`（rust） — Copyright (c) 2014 The Rust Project Developers；Copyright (c) 2018 Ashley Mannix, Christopher Armstrong, Dylan DPC, Hunar Roop Kahlon
- `window-vibrancy@0.6.0`（rust） — 版权行见上游：https://github.com/tauri-apps/tauri-plugin-vibrancy
- `wry@0.55.1`（rust） — 版权行见上游：https://github.com/tauri-apps/wry
- `zeroize@1.9.0`（rust） — 版权行见上游：https://github.com/RustCrypto/utils

### Apache-2.0 WITH LLVM-exception（1 个）

- `target-lexicon@0.12.16`（rust） — 版权行见上游：https://github.com/bytecodealliance/target-lexicon

### Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT（5 个）

- `linux-raw-sys@0.12.1`（rust） — 版权行见上游：https://github.com/sunfishcode/linux-raw-sys
- `rustix@1.1.5`（rust） — 版权行见上游：https://github.com/bytecodealliance/rustix
- `wasi@0.11.1+wasi-snapshot-preview1`（rust） — 版权行见上游：https://github.com/bytecodealliance/wasi
- `wasip2@1.0.4+wasi-0.2.12`（rust） — 版权行见上游：https://github.com/bytecodealliance/wasi-rs
- `wit-bindgen@0.57.1`（rust） — 版权行见上游：https://github.com/bytecodealliance/wit-bindgen

### Apache-2.0/MIT（3 个）

- `dbus@0.9.12`（rust） — Copyright 2014-2018 David Henningsson <diwic@ubuntu.com> and other contributors；Copyright (c) 2014-2018 David Henningsson <diwic@ubuntu.com> and other contributors
- `libdbus-sys@0.2.7`（rust） — Copyright 2014-2018 David Henningsson <diwic@ubuntu.com> and other contributors；Copyright (c) 2014-2018 David Henningsson <diwic@ubuntu.com> and other contributors
- `signal-hook@0.3.18`（rust） — Copyright (c) 2017 tokio-jsonrpc developers

### BSD-2-Clause（5 个）

- `entities@6.0.1`（npm） — 版权行见上游：Felix Boehm <me@feedic.com> / git://github.com/fb55/entities
- `entities@7.0.1`（npm） — 版权行见上游：Felix Boehm <me@feedic.com> / https://github.com/fb55/entities
- `entities@8.0.0`（npm） — 版权行见上游：Felix Boehm <me@feedic.com> / https://github.com/fb55/entities
- `webidl-conversions@8.0.1`（npm） — Copyright (c) 2014, Domenic Denicola
- `pygments@2.21.0`（python） — Copyright (c) 2006-2022 by the respective authors (see AUTHORS file).

### BSD-3-Clause（24 个）

- `highlight.js@11.11.1`（npm） — Copyright (c) 2006, Ivan Sagalaev.
- `source-map-js@1.2.1`（npm） — Copyright (c) 2009-2011, Mozilla Foundation and contributors
- `tough-cookie@6.0.2`（npm） — Copyright (c) 2015, Salesforce.com, Inc.
- `click@8.5.0`（python） — Copyright 2014 Pallets
- `colorama@0.4.6`（python） — Copyright (c) 2010 Jonathan Hartley
- `fsspec@2026.7.0`（python） — 版权行见上游：https://filesystem-spec.readthedocs.io/en/latest/changelog.html
- `httpcore@1.0.9`（python） — Copyright © 2020, [Encode OSS Ltd](https://www.encode.io/).
- `httpcore2@2.12.0`（python） — Copyright © 2020, [Encode OSS Ltd](https://www.encode.io/).
- `httpx@0.28.1`（python） — Copyright © 2019, [Encode OSS Ltd](https://www.encode.io/).
- `httpx2@2.12.0`（python） — Copyright © 2019, [Encode OSS Ltd](https://www.encode.io/).
- `httpx2-jsfetch@1.0`（python） — Copyright © 2019, [Encode OSS Ltd](https://www.encode.io/).
- `idna@3.19`（python） — Copyright (c) 2013-2026, Kim Davies and contributors.
- `protobuf@7.36.1`（python） — Copyright 2008 Google Inc. All rights reserved.
- `pycparser@3.0`（python） — 版权行见上游：https://pypi.org/project/pycparser/
- `pypdf@6.20.0`（python） — Copyright (c) 2006-2008, Mathieu Fenniak
- `python-dotenv@1.2.3`（python） — Copyright (c) 2014, Saurabh Kumar (python-dotenv), 2013, Ted Tieken (django-dotenv-rw), 2013, Jacob Kaplan-Moss (django-dotenv)
- `pywin32-ctypes@0.2.3`（python） — Copyright (c) 2014, Enthought, Inc.
- `secretstorage@3.5.0`（python） — 版权行见上游：https://pypi.org/project/secretstorage/
- `starlette@1.6.0`（python） — Copyright © 2018, [Encode OSS Ltd](https://www.encode.io/).
- `uvicorn@0.52.4`（python） — Copyright © 2017-present, [Encode OSS Ltd](https://www.encode.io/).
- `websockets@17.1`（python） — 版权行见上游：Aymeric Augustin <aymeric.augustin@m4x.org> / https://github.com/python-websockets/websockets
- `alloc-no-stdlib@2.0.4`（rust） — Copyright (c) 2016 Dropbox, Inc.
- `alloc-stdlib@0.2.4`（rust） — 版权行见上游：https://github.com/dropbox/rust-alloc-no-stdlib
- `subtle@2.6.1`（rust） — Copyright (c) 2016-2017 Isis Agora Lovecruft, Henry de Valence. All rights reserved.；Copyright (c) 2016-2024 Isis Agora Lovecruft. All rights reserved.

### BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0（2 个）

- `numpy@2.4.6`（python） — Copyright (c) 2005-2025, NumPy Developers.；Copyright (c) 2011-2014, The OpenBLAS Project
- `numpy@2.5.3`（python） — Copyright (c) 2005-2025, NumPy Developers.；Copyright (c) 2011-2014, The OpenBLAS Project — 本机 venv 装的是 numpy 2.4.6（py3.11）；2.5.3 只出现在 py>=3.12 的锁记录里，版权行取自同版本 dist-info 的许可证文件（人工确认）

### BSD-3-Clause AND MIT（1 个）

- `brotli@8.0.4`（rust） — Copyright (c) 2016 Dropbox, Inc.；Copyright (c) 2009, 2010, 2013-2016 by the Brotli Authors.

### BSD-3-Clause OR MIT OR Apache-2.0（2 个）

- `num_enum@0.7.6`（rust） — 版权行见上游：https://github.com/illicitonion/num_enum
- `num_enum_derive@0.7.6`（rust） — 版权行见上游：https://github.com/illicitonion/num_enum

### BSD-3-Clause/MIT（1 个）

- `brotli-decompressor@5.0.3`（rust） — Copyright (c) 2016 Dropbox, Inc.

### BlueOak-1.0.0（5 个）

- `jackspeak@3.4.3`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> / https://github.com/isaacs/jackspeak
- `lru-cache@11.5.2`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> / ssh://git@github.com/isaacs/node-lru-cache
- `minipass@7.1.3`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / https://github.com/isaacs/minipass
- `package-json-from-dist@1.0.1`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> (https://izs.me) / https://github.com/isaacs/package-json-from-dist
- `path-scurry@1.11.1`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> (https://blog.izs.me) / https://github.com/isaacs/path-scurry

### CC0-1.0（1 个）

- `mdn-data@2.27.1`（npm） — 版权行见上游：Mozilla Developer Network / https://github.com/mdn/data

### CC0-1.0 OR MIT-0 OR Apache-2.0（1 个）

- `dunce@1.0.5`（rust） — 版权行见上游：https://gitlab.com/kornelski/dunce

### CDLA-Permissive-2.0（1 个）

- `webpki-root-certs@1.0.9`（rust） — 版权行见上游：https://github.com/rustls/webpki-roots

### ISC（20 个）

- `@isaacs/cliui@8.0.2`（npm） — Copyright (c) 2015, Contributors
- `@ungap/structured-clone@1.3.3`（npm） — Copyright (c) 2021, Andrea Giammarchi, @WebReflection
- `abbrev@2.0.0`（npm） — Copyright Isaac Z. Schlueter and Contributors
- `foreground-child@3.3.1`（npm） — Copyright (c) 2015-2023 Isaac Z. Schlueter and Contributors
- `glob@10.4.5`（npm） — Copyright (c) 2009-2023 Isaac Z. Schlueter and Contributors
- `ini@1.3.8`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / git://github.com/isaacs/ini
- `isexe@2.0.0`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / https://github.com/isaacs/isexe
- `lru-cache@10.4.3`（npm） — Copyright (c) 2010-2023 Isaac Z. Schlueter and Contributors
- `minimatch@9.0.9`（npm） — Copyright (c) 2011-2023 Isaac Z. Schlueter and Contributors
- `nopt@7.2.1`（npm） — 版权行见上游：GitHub Inc. / https://github.com/npm/nopt
- `picocolors@1.1.1`（npm） — Copyright (c) 2021-2024 Oleksii Raspopov, Kostiantyn Denysov, Anton Verinov
- `proto-list@1.2.4`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / https://github.com/isaacs/proto-list
- `saxes@6.0.0`（npm） — 版权行见上游：Louis-Dominique Dubeau <ldd@lddubeau.com> / https://github.com/lddubeau/saxes
- `semver@7.8.5`（npm） — 版权行见上游：GitHub Inc. / https://github.com/npm/node-semver
- `siginfo@2.0.0`（npm） — Copyright (c) 2017, Emil Bay <github@tixz.dk>
- `signal-exit@4.1.0`（npm） — 版权行见上游：Ben Coe <ben@npmjs.com> / https://github.com/tapjs/signal-exit
- `which@2.0.2`（npm） — 版权行见上游：Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me) / git://github.com/isaacs/node-which
- `libloading@0.7.4`（rust） — Copyright © 2015, Simonas Kazlauskas
- `rustls-webpki@0.103.15`（rust） — Copyright 2015 Brian Smith.
- `untrusted@0.9.0`（rust） — 版权行见上游：https://github.com/briansmith/untrusted

### MIT（448 个）

- `bge-small-zh-v1.5@fp32`（bundled） — Copyright (c) BAAI (Beijing Academy of Artificial Intelligence) — 内置中文嵌入模型（ONNX 转换版来自社区仓库 Xenova/bge-small-zh-v1.5），随安装包分发；完整声明见 resources/models/bge-small-zh-v1.5/ 下的 LICENSE-BAAI-bge-small-zh-v1.5.txt 与 NOTICE.txt。
- `@asamuzakjp/css-color@5.1.11`（npm） — Copyright (c) 2024 asamuzaK (Kazz)
- `@asamuzakjp/dom-selector@7.1.1`（npm） — Copyright (c) 2023 asamuzaK (Kazz)
- `@asamuzakjp/generational-cache@1.0.1`（npm） — Copyright (c) 2026 asamuzaK (Kazz)
- `@asamuzakjp/nwsapi@2.3.9`（npm） — Copyright (c) 2007-2019 Diego Perini (http://www.iport.it/)
- `@babel/helper-string-parser@7.29.7`（npm） — Copyright (c) 2014-present Sebastian McKenzie and other contributors
- `@babel/helper-validator-identifier@7.29.7`（npm） — Copyright (c) 2014-present Sebastian McKenzie and other contributors
- `@babel/parser@7.29.8`（npm） — Copyright (C) 2012-2014 by various contributors (see AUTHORS)
- `@babel/types@7.29.8`（npm） — Copyright (c) 2014-present Sebastian McKenzie and other contributors
- `@bramus/specificity@2.4.2`（npm） — Copyright (c) 2022 Bramus Van Damme - https://www.bram.us/
- `@csstools/css-calc@3.3.0`（npm） — 版权行见上游：https://github.com/csstools/postcss-plugins
- `@csstools/css-color-parser@4.1.10`（npm） — 版权行见上游：https://github.com/csstools/postcss-plugins
- `@csstools/css-parser-algorithms@4.0.0`（npm） — 版权行见上游：https://github.com/csstools/postcss-plugins
- `@csstools/css-tokenizer@4.0.0`（npm） — 版权行见上游：https://github.com/csstools/postcss-plugins
- `@esbuild/aix-ppc64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/aix-ppc64
- `@esbuild/android-arm@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/android-arm
- `@esbuild/android-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/android-arm64
- `@esbuild/android-x64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/android-x64
- `@esbuild/darwin-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/darwin-arm64
- `@esbuild/darwin-x64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/darwin-x64
- `@esbuild/freebsd-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/freebsd-arm64
- `@esbuild/freebsd-x64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/freebsd-x64
- `@esbuild/linux-arm@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-arm
- `@esbuild/linux-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-arm64
- `@esbuild/linux-ia32@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-ia32
- `@esbuild/linux-loong64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-loong64
- `@esbuild/linux-mips64el@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-mips64el
- `@esbuild/linux-ppc64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-ppc64
- `@esbuild/linux-riscv64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-riscv64
- `@esbuild/linux-s390x@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-s390x
- `@esbuild/linux-x64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/linux-x64
- `@esbuild/netbsd-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/netbsd-arm64
- `@esbuild/netbsd-x64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/netbsd-x64
- `@esbuild/openbsd-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/openbsd-arm64
- `@esbuild/openbsd-x64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/openbsd-x64
- `@esbuild/openharmony-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/openharmony-arm64
- `@esbuild/sunos-x64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/sunos-x64
- `@esbuild/win32-arm64@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/win32-arm64
- `@esbuild/win32-ia32@0.25.12`（npm） — 版权行见上游：https://www.npmjs.com/package/@esbuild/win32-ia32
- `@esbuild/win32-x64@0.25.12`（npm） — 版权行见上游：https://github.com/evanw/esbuild
- `@exodus/bytes@1.15.1`（npm） — Copyright (c) 2024-2025 Exodus Movement
- `@jridgewell/sourcemap-codec@1.5.5`（npm） — 版权行见上游：Justin Ridgewell <justin@ridgewell.name> / https://github.com/jridgewell/sourcemaps
- `@napi-rs/canvas@1.0.11`（npm） — Copyright (c) 2020 lynweklm@gmail.com
- `@napi-rs/canvas-android-arm64@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-android-arm64
- `@napi-rs/canvas-darwin-arm64@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-darwin-arm64
- `@napi-rs/canvas-darwin-x64@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-darwin-x64
- `@napi-rs/canvas-linux-arm-gnueabihf@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-linux-arm-gnueabihf
- `@napi-rs/canvas-linux-arm64-gnu@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-linux-arm64-gnu
- `@napi-rs/canvas-linux-arm64-musl@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-linux-arm64-musl
- `@napi-rs/canvas-linux-riscv64-gnu@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-linux-riscv64-gnu
- `@napi-rs/canvas-linux-x64-gnu@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-linux-x64-gnu
- `@napi-rs/canvas-linux-x64-musl@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-linux-x64-musl
- `@napi-rs/canvas-win32-arm64-msvc@1.0.11`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/canvas-win32-arm64-msvc
- `@napi-rs/canvas-win32-x64-msvc@1.0.11`（npm） — 版权行见上游：https://github.com/Brooooooklyn/canvas
- `@napi-rs/lzma-linux-x64-gnu@1.5.1`（npm） — 版权行见上游：https://www.npmjs.com/package/@napi-rs/lzma-linux-x64-gnu
- `@one-ini/wasm@0.1.1`（npm） — Copyright (c) 2019 Jed Mao
- `@pkgjs/parseargs@0.11.0`（npm） — 版权行见上游：git@github.com:pkgjs/parseargs
- `@rollup/rollup-android-arm-eabi@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-android-arm-eabi
- `@rollup/rollup-android-arm64@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-android-arm64
- `@rollup/rollup-darwin-arm64@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-darwin-arm64
- `@rollup/rollup-darwin-x64@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-darwin-x64
- `@rollup/rollup-freebsd-arm64@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-freebsd-arm64
- `@rollup/rollup-freebsd-x64@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-freebsd-x64
- `@rollup/rollup-linux-arm-gnueabihf@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-arm-gnueabihf
- `@rollup/rollup-linux-arm-musleabihf@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-arm-musleabihf
- `@rollup/rollup-linux-arm64-gnu@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-arm64-gnu
- `@rollup/rollup-linux-arm64-musl@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-arm64-musl
- `@rollup/rollup-linux-loong64-gnu@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-loong64-gnu
- `@rollup/rollup-linux-loong64-musl@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-loong64-musl
- `@rollup/rollup-linux-ppc64-gnu@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-ppc64-gnu
- `@rollup/rollup-linux-ppc64-musl@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-ppc64-musl
- `@rollup/rollup-linux-riscv64-gnu@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-riscv64-gnu
- `@rollup/rollup-linux-riscv64-musl@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-riscv64-musl
- `@rollup/rollup-linux-s390x-gnu@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-s390x-gnu
- `@rollup/rollup-linux-x64-gnu@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-x64-gnu
- `@rollup/rollup-linux-x64-musl@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-linux-x64-musl
- `@rollup/rollup-openbsd-x64@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-openbsd-x64
- `@rollup/rollup-openharmony-arm64@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-openharmony-arm64
- `@rollup/rollup-win32-arm64-msvc@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-win32-arm64-msvc
- `@rollup/rollup-win32-ia32-msvc@4.62.4`（npm） — 版权行见上游：https://www.npmjs.com/package/@rollup/rollup-win32-ia32-msvc
- `@rollup/rollup-win32-x64-gnu@4.62.4`（npm） — 版权行见上游：Lukas Taegert-Atkinson / https://github.com/rollup/rollup
- `@rollup/rollup-win32-x64-msvc@4.62.4`（npm） — 版权行见上游：Lukas Taegert-Atkinson / https://github.com/rollup/rollup
- `@standard-schema/spec@1.1.0`（npm） — 版权行见上游：Colin McDonnell / https://github.com/standard-schema/standard-schema
- `@tanstack/virtual-core@3.17.7`（npm） — Copyright (c) 2021-present Tanner Linsley
- `@tanstack/vue-virtual@3.13.35`（npm） — Copyright (c) 2021-present Tanner Linsley
- `@tweenjs/tween.js@23.1.3`（npm） — Copyright (c) 2010-2012 Tween.js authors.
- `@types/chai@5.2.3`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/debug@4.1.13`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/deep-eql@4.0.2`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/estree@1.0.9`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/hast@3.0.5`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/mdast@4.0.4`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/ms@2.1.0`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/node@26.2.0`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/stats.js@0.17.4`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/three@0.185.3`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/unist@2.0.11`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/unist@3.0.3`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/webxr@0.5.24`（npm） — 版权行见上游：https://github.com/DefinitelyTyped/DefinitelyTyped
- `@vitejs/plugin-vue@5.2.4`（npm） — Copyright (c) 2019-present, Yuxi (Evan) You and Vite contributors
- `@vitest/expect@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `@vitest/mocker@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `@vitest/pretty-format@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `@vitest/runner@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `@vitest/snapshot@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `@vitest/spy@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `@vitest/utils@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `@volar/language-core@2.4.15`（npm） — Copyright (c) 2021-present Johnson Chu
- `@volar/source-map@2.4.15`（npm） — Copyright (c) 2021-present Johnson Chu
- `@volar/typescript@2.4.15`（npm） — Copyright (c) 2021-present Johnson Chu
- `@vue/compiler-core@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/compiler-dom@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/compiler-sfc@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/compiler-ssr@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/compiler-vue2@2.7.16`（npm） — 版权行见上游：Evan You / https://github.com/vuejs/vue
- `@vue/devtools-api@6.6.4`（npm） — 版权行见上游：Guillaume Chau / https://github.com/vuejs/vue-devtools
- `@vue/language-core@2.2.12`（npm） — Copyright (c) 2021-present Johnson Chu
- `@vue/reactivity@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/runtime-core@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/runtime-dom@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/server-renderer@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/shared@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `@vue/test-utils@2.4.11`（npm） — Copyright (c) 2021-present vuejs
- `alien-signals@1.0.13`（npm） — Copyright (c) 2024-present Johnson Chu
- `ansi-regex@5.0.1`（npm） — 版权行见上游：Sindre Sorhus / chalk/ansi-regex
- `ansi-regex@6.2.2`（npm） — 版权行见上游：Sindre Sorhus / chalk/ansi-regex
- `ansi-styles@4.3.0`（npm） — 版权行见上游：Sindre Sorhus / chalk/ansi-styles
- `ansi-styles@6.2.3`（npm） — 版权行见上游：Sindre Sorhus / chalk/ansi-styles
- `assertion-error@2.0.1`（npm） — Copyright (c) 2013 Jake Luer jake@qualiancy.com (http://qualiancy.com)
- `bail@2.0.2`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `balanced-match@1.0.2`（npm） — Copyright (c) 2013 Julian Gruber &lt;julian@juliangruber.com&gt;
- `bidi-js@1.0.3`（npm） — Copyright (c) 2021 Jason Johnston
- `brace-expansion@2.1.4`（npm） — Copyright (c) 2013 Julian Gruber <julian@juliangruber.com>
- `ccount@2.0.1`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `chai@6.2.2`（npm） — Copyright (c) 2017 Chai.js Assertion Library
- `character-entities@2.0.2`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `character-entities-html4@2.1.0`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `character-entities-legacy@3.0.0`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `character-reference-invalid@2.0.1`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `color-convert@2.0.1`（npm） — Copyright (c) 2011-2016 Heather Arthur <fayearthur@gmail.com>
- `color-name@1.1.4`（npm） — Copyright (c) 2015 Dmitry Ivanov
- `comma-separated-tokens@2.0.3`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `commander@10.0.1`（npm） — Copyright (c) 2011 TJ Holowaychuk <tj@vision-media.ca>
- `config-chain@1.1.13`（npm） — Copyright (c) 2011 Dominic Tarr
- `convert-source-map@2.0.0`（npm） — Copyright 2013 Thorsten Lorenz.
- `cross-spawn@7.0.6`（npm） — Copyright (c) 2018 Made With MOXY Lda <hello@moxy.studio>
- `css-tree@3.2.1`（npm） — Copyright (C) 2016-2026 by Roman Dvornov
- `csstype@3.2.3`（npm） — Copyright (c) 2017-2018 Fredrik Nicol
- `data-urls@7.0.0`（npm） — 版权行见上游：Domenic Denicola <d@domenic.me> (https://domenic.me/) / https://github.com/jsdom/data-urls
- `de-indent@1.0.2`（npm） — 版权行见上游：Evan You / https://github.com/yyx990803/de-indent
- `debug@4.4.3`（npm） — Copyright (c) 2014-2017 TJ Holowaychuk <tj@vision-media.ca>；Copyright (c) 2018-2021 Josh Junon
- `decimal.js@10.6.0`（npm） — Copyright (c) 2025 Michael Mclaughlin
- `decode-named-character-reference@1.3.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/decode-named-character-reference
- `dequal@2.0.3`（npm） — 版权行见上游：Luke Edwards / lukeed/dequal
- `devlop@1.1.0`（npm） — Copyright (c) 2023 Titus Wormer <tituswormer@gmail.com>
- `eastasianwidth@0.2.0`（npm） — 版权行见上游：Masaki Komagata / git://github.com/komagata/eastasianwidth
- `editorconfig@1.0.7`（npm） — Copyright © 2012 EditorConfig Team
- `emoji-regex@8.0.0`（npm） — Copyright Mathias Bynens <https://mathiasbynens.be/>
- `emoji-regex@9.2.2`（npm） — Copyright Mathias Bynens <https://mathiasbynens.be/>
- `es-module-lexer@2.3.1`（npm） — Copyright (C) 2018-2022 Guy Bedford
- `esbuild@0.25.12`（npm） — Copyright (c) 2020 Evan Wallace
- `escape-string-regexp@5.0.0`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/escape-string-regexp
- `estree-walker@2.0.2`（npm） — Copyright (c) 2015-20 [these people](https://github.com/Rich-Harris/estree-walker/graphs/contributors)
- `estree-walker@3.0.3`（npm） — Copyright (c) 2015-20 [these people](https://github.com/Rich-Harris/estree-walker/graphs/contributors)
- `extend@3.0.2`（npm） — Copyright (c) 2014 Stefan Thomas
- `fdir@6.5.0`（npm） — Copyright 2023 Abdullah Atta
- `fflate@0.8.3`（npm） — Copyright (c) 2026 Arjun Barrett
- `fsevents@2.3.3`（npm） — 版权行见上游：https://www.npmjs.com/package/fsevents
- `hast-util-from-parse5@8.0.3`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-from-parse5
- `hast-util-parse-selector@4.0.0`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `hast-util-raw@9.1.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-raw
- `hast-util-to-html@9.0.5`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-to-html
- `hast-util-to-parse5@8.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-to-parse5
- `hast-util-whitespace@3.0.0`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `hastscript@9.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hastscript
- `he@1.2.0`（npm） — Copyright Mathias Bynens <https://mathiasbynens.be/>
- `html-encoding-sniffer@6.0.0`（npm） — 版权行见上游：Domenic Denicola <d@domenic.me> (https://domenic.me/) / https://github.com/jsdom/html-encoding-sniffer
- `html-void-elements@3.0.0`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `is-alphabetical@2.0.1`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `is-alphanumerical@2.0.1`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `is-decimal@2.0.1`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `is-fullwidth-code-point@3.0.0`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/is-fullwidth-code-point
- `is-hexadecimal@2.0.1`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `is-plain-obj@4.1.0`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/is-plain-obj
- `is-potential-custom-element-name@1.0.1`（npm） — Copyright Mathias Bynens <https://mathiasbynens.be/>
- `js-beautify@1.15.4`（npm） — Copyright (c) 2007-2018 Einar Lielmanis, Liam Newman, and contributors.
- `js-cookie@3.0.8`（npm） — Copyright (c) 2018 Copyright 2018 Klaus Hartl, Fagner Brack, GitHub Contributors
- `jsdom@29.1.1`（npm） — Copyright (c) 2010 Elijah Insua
- `longest-streak@3.1.0`（npm） — Copyright (c) 2015 Titus Wormer <mailto:tituswormer@gmail.com>
- `magic-string@0.30.21`（npm） — Copyright 2018 Rich Harris
- `markdown-table@3.0.4`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/markdown-table
- `mdast-util-directive@3.1.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-directive
- `mdast-util-find-and-replace@3.0.2`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-find-and-replace
- `mdast-util-from-markdown@2.0.3`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-from-markdown
- `mdast-util-gfm@3.1.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-gfm
- `mdast-util-gfm-autolink-literal@2.0.1`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `mdast-util-gfm-footnote@2.1.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-gfm-footnote
- `mdast-util-gfm-strikethrough@2.0.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `mdast-util-gfm-table@2.0.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `mdast-util-gfm-task-list-item@2.0.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `mdast-util-phrasing@4.1.0`（npm） — Copyright (c) 2017 Titus Wormer <tituswormer@gmail.com>；Copyright (c) 2017 Victor Felder <victor@draft.li>
- `mdast-util-to-hast@13.2.1`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `mdast-util-to-markdown@2.1.2`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-to-markdown
- `mdast-util-to-string@4.0.0`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `meshoptimizer@1.1.1`（npm） — Copyright (c) 2016-2026 Arseny Kapoulkine
- `micromark@4.0.2`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark
- `micromark-core-commonmark@2.0.3`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-core-commonmark
- `micromark-extension-directive@4.0.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / micromark/micromark-extension-directive
- `micromark-extension-gfm@3.0.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `micromark-extension-gfm-autolink-literal@2.1.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `micromark-extension-gfm-footnote@2.1.0`（npm） — Copyright (c) 2021 Titus Wormer <tituswormer@gmail.com>
- `micromark-extension-gfm-strikethrough@2.1.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `micromark-extension-gfm-table@2.1.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / micromark/micromark-extension-gfm-table
- `micromark-extension-gfm-tagfilter@2.0.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `micromark-extension-gfm-task-list-item@2.1.0`（npm） — Copyright (c) 2020 Titus Wormer <tituswormer@gmail.com>
- `micromark-factory-destination@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-destination
- `micromark-factory-label@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-label
- `micromark-factory-space@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-space
- `micromark-factory-title@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-title
- `micromark-factory-whitespace@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-whitespace
- `micromark-util-character@2.1.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-character
- `micromark-util-chunked@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-chunked
- `micromark-util-classify-character@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-classify-character
- `micromark-util-combine-extensions@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-combine-extensions
- `micromark-util-decode-numeric-character-reference@2.0.2`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-decode-numeric-character-reference
- `micromark-util-decode-string@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-decode-string
- `micromark-util-encode@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-encode
- `micromark-util-html-tag-name@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-html-tag-name
- `micromark-util-normalize-identifier@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-normalize-identifier
- `micromark-util-resolve-all@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-resolve-all
- `micromark-util-sanitize-uri@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-sanitize-uri
- `micromark-util-subtokenize@2.1.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-subtokenize
- `micromark-util-symbol@2.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-symbol
- `micromark-util-types@2.0.2`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-types
- `ms@2.1.3`（npm） — Copyright (c) 2020 Vercel, Inc.
- `muggle-string@0.4.1`（npm） — Copyright (c) 2022-present Johnson Chu
- `nanoid@3.3.16`（npm） — Copyright 2017 Andrey Sitnik <andrey@sitnik.ru>
- `obug@2.1.4`（npm） — Copyright (c) 2014-2017 TJ Holowaychuk <tj@vision-media.ca>；Copyright (c) 2018-2021 Josh Junon
- `parse-entities@4.0.2`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/parse-entities
- `parse5@7.3.0`（npm） — 版权行见上游：Ivan Nikulin <ifaaan@gmail.com> (https://github.com/inikulin) / git://github.com/inikulin/parse5
- `parse5@8.0.1`（npm） — 版权行见上游：Ivan Nikulin <ifaaan@gmail.com> (https://github.com/inikulin) / git://github.com/inikulin/parse5
- `path-browserify@1.0.1`（npm） — Copyright (c) 2013 James Halliday
- `path-key@3.1.1`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/path-key
- `pathe@2.0.3`（npm） — Copyright Joyent, Inc. and other Node contributors.；Copyright (c) 2023-present Fabio Spampinato
- `picomatch@4.0.5`（npm） — Copyright (c) 2017-present, Jon Schlinkert.
- `pinia@2.3.1`（npm） — 版权行见上游：Eduardo San Martin Morote / https://github.com/vuejs/pinia
- `postcss@8.5.25`（npm） — Copyright 2013 Andrey Sitnik <andrey@sitnik.es>
- `property-information@7.2.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/property-information
- `punycode@2.3.1`（npm） — Copyright Mathias Bynens <https://mathiasbynens.be/>
- `rehype-raw@7.0.0`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `remark-directive@4.0.0`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / remarkjs/remark-directive
- `remark-gfm@4.0.1`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / remarkjs/remark-gfm
- `remark-parse@11.0.0`（npm） — Copyright (c) 2014 Titus Wormer <tituswormer@gmail.com>
- `remark-stringify@11.0.0`（npm） — Copyright (c) 2014 Titus Wormer <tituswormer@gmail.com>
- `require-from-string@2.0.2`（npm） — 版权行见上游：Vsevolod Strukchinsky / floatdrop/require-from-string
- `rollup@4.62.4`（npm） — Copyright (c) 2017 [these people](https://github.com/rollup/rollup/graphs/contributors)
- `shebang-command@2.0.0`（npm） — 版权行见上游：Kevin Mårtensson / kevva/shebang-command
- `shebang-regex@3.0.0`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/shebang-regex
- `space-separated-tokens@2.0.2`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `stackback@0.0.2`（npm） — 版权行见上游：Roman Shtylman <shtylman@gmail.com> / git://github.com/shtylman/node-stackback
- `std-env@4.2.0`（npm） — 版权行见上游：unjs/std-env
- `string-width@4.2.3`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/string-width
- `string-width@5.1.2`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/string-width
- `string-width-cjs@4.2.3`（npm） — 版权行见上游：Sindre Sorhus / sindresorhus/string-width
- `stringify-entities@4.0.4`（npm） — Copyright (c) 2015 Titus Wormer <mailto:tituswormer@gmail.com>
- `strip-ansi@6.0.1`（npm） — 版权行见上游：Sindre Sorhus / chalk/strip-ansi
- `strip-ansi@7.2.0`（npm） — 版权行见上游：Sindre Sorhus / chalk/strip-ansi
- `strip-ansi-cjs@6.0.1`（npm） — 版权行见上游：Sindre Sorhus / chalk/strip-ansi
- `symbol-tree@3.2.4`（npm） — Copyright (c) 2015 Joris van der Wel
- `three@0.185.1`（npm） — Copyright © 2010-2026 three.js authors
- `tinybench@2.9.0`（npm） — Copyright (c) 2022 Tinylibs
- `tinyexec@1.3.0`（npm） — Copyright (c) 2024 Tinylibs
- `tinyglobby@0.2.17`（npm） — Copyright (c) 2024 Madeline Gurriarán
- `tinyrainbow@3.1.1`（npm） — Copyright (c) 2022 Tinylibs
- `tldts@7.4.10`（npm） — Copyright (c) 2017 Thomas Parisot, 2018 Rémi Berson
- `tldts-core@7.4.10`（npm） — Copyright (c) 2017 Thomas Parisot, 2018 Rémi Berson
- `tr46@6.0.0`（npm） — 版权行见上游：Sebastian Mayr <npm@smayr.name> / https://github.com/jsdom/tr46
- `trim-lines@3.0.1`（npm） — Copyright (c) 2015 Titus Wormer <mailto:tituswormer@gmail.com>
- `trough@2.2.0`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `undici@7.29.0`（npm） — 版权行见上游：https://github.com/nodejs/undici
- `undici-types@8.3.0`（npm） — 版权行见上游：https://github.com/nodejs/undici
- `unified@11.0.5`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `unist-util-is@6.0.1`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `unist-util-position@5.0.0`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `unist-util-stringify-position@4.0.0`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `unist-util-visit@5.1.0`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `unist-util-visit-parents@6.0.2`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `vfile@6.0.3`（npm） — Copyright (c) 2015 Titus Wormer <tituswormer@gmail.com>
- `vfile-location@5.0.3`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `vfile-message@4.0.3`（npm） — 版权行见上游：Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / vfile/vfile-message
- `vite@6.4.3`（npm） — Copyright (c) 2019-present, VoidZero Inc. and Vite contributors
- `vitest@4.1.10`（npm） — Copyright (c) 2021-Present VoidZero Inc. and Vitest contributors
- `vscode-uri@3.1.0`（npm） — 版权行见上游：Microsoft / https://github.com/microsoft/vscode-uri
- `vue@3.5.40`（npm） — Copyright (c) 2018-present, Yuxi (Evan) You
- `vue-component-type-helpers@3.3.9`（npm） — Copyright (c) 2023-present Johnson Chu
- `vue-demi@0.14.10`（npm） — Copyright (c) 2020-present, Anthony Fu
- `vue-router@4.6.4`（npm） — 版权行见上游：Eduardo San Martin Morote / https://github.com/vuejs/router
- `vue-tsc@2.2.12`（npm） — Copyright (c) 2021-present Johnson Chu
- `w3c-xmlserializer@5.0.0`（npm） — 版权行见上游：jsdom/w3c-xmlserializer
- `web-namespaces@2.0.1`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `whatwg-mimetype@5.0.0`（npm） — 版权行见上游：Domenic Denicola <d@domenic.me> (https://domenic.me/) / jsdom/whatwg-mimetype
- `whatwg-url@16.0.1`（npm） — 版权行见上游：Sebastian Mayr <github@smayr.name> / https://github.com/jsdom/whatwg-url
- `why-is-node-running@2.3.0`（npm） — Copyright (c) 2016 Mathias Buus
- `wrap-ansi@8.1.0`（npm） — 版权行见上游：Sindre Sorhus / chalk/wrap-ansi
- `wrap-ansi-cjs@7.0.0`（npm） — 版权行见上游：Sindre Sorhus / chalk/wrap-ansi
- `xmlchars@2.2.0`（npm） — 版权行见上游：Louis-Dominique Dubeau <ldd@lddubeau.com> / https://github.com/lddubeau/xmlchars
- `zwitch@2.0.4`（npm） — Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `annotated-doc@0.0.5`（python） — Copyright (c) 2025 Sebastián Ramírez
- `annotated-types@0.8.0`（python） — Copyright (c) 2022 the contributors
- `anyio@4.15.1`（python） — Copyright (c) 2018 Alex Grönholm
- `backports-tarfile@1.2.0`（python） — 版权行见上游：https://pypi.org/project/backports-tarfile/
- `beautifulsoup4@4.15.0`（python） — 版权行见上游：Leonard Richardson <leonardr@segfault.org> / https://www.crummy.com/software/BeautifulSoup/bs4/download/
- `charset-normalizer@3.5.1`（python） — Copyright (c) 2025 TAHRI Ahmed R.
- `fastapi@0.141.1`（python） — Copyright (c) 2018 Sebastián Ramírez
- `filelock@3.32.6`（python） — Copyright (c) 2025 Bernát Gábor and contributors
- `h11@0.16.0`（python） — Copyright (c) 2016 Nathaniel J. Smith <njs@pobox.com> and other contributors
- `httptools@0.8.0`（python） — Copyright (c) 2015 MagicStack Inc. http://magic.io
- `iniconfig@2.3.0`（python） — Copyright (c) 2010 - 2023 Holger Krekel and others
- `jaraco-classes@3.4.0`（python） — 版权行见上游：Jason R. Coombs
- `jaraco-context@6.1.2`（python） — 版权行见上游：Jason R. Coombs <jaraco@jaraco.com> / https://github.com/jaraco/jaraco.context
- `jaraco-functools@4.6.0`（python） — 版权行见上游：Jason R. Coombs <jaraco@jaraco.com> / https://github.com/jaraco/jaraco.functools
- `jeepney@0.9.0`（python） — 版权行见上游：https://pypi.org/project/jeepney/
- `jiter@0.16.0`（python） — 版权行见上游：Samuel Colvin <s@muelcolvin.com>
- `keyring@25.7.0`（python） — 版权行见上游：Kang Zhang <jobo.zh@gmail.com> / https://github.com/jaraco/keyring
- `more-itertools@11.1.0`（python） — Copyright (c) 2012 Erik Rose
- `onnxruntime@1.30.0`（python） — 版权行见上游：Microsoft Corporation
- `pluggy@1.6.0`（python） — Copyright (c) 2015 holger krekel (rather uses bitbucket/hpk42)
- `pydantic@2.13.5`（python） — 版权行见上游：Samuel Colvin <s@muelcolvin.com>, Eric Jolibois <em.jolibois@gmail.com>, Hasan Ramezani <hasan.r67@gmail.com>, Adrian Garcia Badaracco <1755071+adriangb@users.noreply.github.com>, Terrence Dorsey <terry@pydantic.dev>, David Montague <david@pydantic.dev>, Serge Matveenko <lig@countzero.co>, Marcelo Trylesinski <marcelotryle@gmail.com>, Sydney Runkle <sydneymarierunkle@gmail.com>, David Hewitt <mail@davidhewitt.dev>, Alex Hall <alex.mojaki@gmail.com>, Victorien Plot <contact@vctrn.dev> / https://github.com/pydantic/pydantic
- `pydantic-core@2.46.5`（python） — Copyright (c) 2022 Samuel Colvin
- `pytest@9.1.1`（python） — Copyright (c) 2004 Holger Krekel and others
- `pyyaml@6.0.3`（python） — Copyright (c) 2017-2021 Ingy döt Net；Copyright (c) 2006-2016 Kirill Simonov
- `soupsieve@2.9.2`（python） — Copyright (c) 2018 - 2026 Isaac Muse <isaacmuse@gmail.com>
- `tiktoken@0.14.0`（python） — Copyright (c) 2022 OpenAI, Shantanu Jain
- `truststore@0.10.4`（python） — Copyright (c) 2022 Seth Michael Larson
- `typing-inspection@0.4.4`（python） — 版权行见上游：Victorien Plot <contact@vctrn.dev> / https://github.com/pydantic/typing-inspection
- `urllib3@2.7.0`（python） — Copyright (c) 2008-2020 Andrey Petrov and contributors.
- `uvloop@0.22.1`（python） — 版权行见上游：https://pypi.org/project/uvloop/
- `watchfiles@1.2.0`（python） — 版权行见上游：Samuel Colvin <s@muelcolvin.com> / https://github.com/samuelcolvin/watchfiles/releases
- `zipp@4.1.0`（python） — 版权行见上游：Jason R. Coombs <jaraco@jaraco.com> / https://github.com/jaraco/zipp
- `atk@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `atk-sys@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `block2@0.6.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `bytes@1.12.1`（rust） — Copyright (c) 2018 Carl Lerche
- `cairo-rs@0.18.5`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `cairo-sys-rs@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `cargo_metadata@0.19.2`（rust） — 版权行见上游：https://github.com/oli-obk/cargo_metadata
- `cfb@0.7.3`（rust） — Copyright (c) 2017 Matthew D. Steele
- `combine@4.6.7`（rust） — 版权行见上游：https://github.com/Marwes/combine
- `darling@0.23.0`（rust） — Copyright (c) 2017 Ted Driggs
- `darling_core@0.23.0`（rust） — Copyright (c) 2017 Ted Driggs
- `darling_macro@0.23.0`（rust） — Copyright (c) 2017 Ted Driggs
- `derive_more@2.1.1`（rust） — Copyright (c) 2016 Jelte Fennema
- `derive_more-impl@2.1.1`（rust） — Copyright (c) 2016 Jelte Fennema
- `dlopen2@0.8.2`（rust） — 版权行见上游：https://github.com/OpenByteDev/dlopen2
- `dlopen2_derive@0.4.3`（rust） — 版权行见上游：https://github.com/OpenByteDev/dlopen2
- `dom_query@0.27.0`（rust） — Copyright (c) 2023 Mykola Humanov
- `embed-resource@3.0.11`（rust） — Copyright (c) 2017 nabijaczleweli
- `fern@0.7.1`（rust） — Copyright (c) 2014-2017 David Ross
- `gdk@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `gdk-pixbuf@0.18.5`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `gdk-pixbuf-sys@0.18.0`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `gdk-sys@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `gdkwayland-sys@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `gdkx11@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `gdkx11-sys@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `generic-array@0.14.7`（rust） — Copyright (c) 2015 Bartłomiej Kamiński
- `gio@0.18.4`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `gio-sys@0.18.1`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `glib@0.18.5`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `glib-macros@0.18.5`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `glib-sys@0.18.1`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `gobject-sys@0.18.0`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `gtk@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `gtk-sys@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `gtk3-macros@0.18.2`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk3-rs
- `http-body@1.1.0`（rust） — Copyright (c) 2019-2026 Sean McArthur & Hyper Contributors
- `http-body-util@0.1.4`（rust） — Copyright (c) 2019-2026 Sean McArthur & Hyper Contributors
- `hyper@1.11.0`（rust） — Copyright (c) 2014-2026 Sean McArthur
- `hyper-util@0.1.20`（rust） — Copyright (c) 2023-2025 Sean McArthur
- `ico@0.5.0`（rust） — Copyright (c) 2018 Matthew D. Steele
- `infer@0.19.0`（rust） — Copyright (c) 2019 Bojan
- `is-docker@0.2.0`（rust） — Copyright (c) 2023 Sean Larkin
- `is-wsl@0.4.0`（rust） — Copyright (c) 2023 Sean Larkin
- `javascriptcore-rs@1.1.2`（rust） — Copyright (c) 2013-2021, The Gtk-rs Project Developers.
- `javascriptcore-rs-sys@1.1.1`（rust） — Copyright (c) 2013-2017, The Gtk-rs Project Developers.
- `libredox@0.1.18`（rust） — 版权行见上游：https://gitlab.redox-os.org/redox-os/libredox.git
- `memoffset@0.9.1`（rust） — Copyright (c) 2017 Gilad Naaman
- `minisign-verify@0.2.5`（rust） — Copyright (c) 2019-2025 Frank Denis；Copyright (c) 2006-2009 Graydon Hoare
- `mio@1.2.2`（rust） — Copyright (c) 2014 Carl Lerche and other MIO contributors
- `new_debug_unreachable@1.0.6`（rust） — Copyright (c) 2015 Jonathan Reem
- `objc2@0.6.4`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-encode@4.1.0`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-foundation@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `open@5.4.0`（rust） — 版权行见上游：https://github.com/Byron/open-rs
- `os_pipe@1.2.3`（rust） — 版权行见上游：https://github.com/oconnor663/os_pipe.rs
- `pango@0.18.3`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `pango-sys@0.18.0`（rust） — 版权行见上游：https://github.com/gtk-rs/gtk-rs-core
- `phf@0.13.1`（rust） — Copyright (c) 2014-2022 Steven Fackler, Yuki Okushi
- `phf_codegen@0.13.1`（rust） — Copyright (c) 2014-2022 Steven Fackler, Yuki Okushi
- `phf_generator@0.13.1`（rust） — Copyright (c) 2014-2022 Steven Fackler, Yuki Okushi
- `phf_macros@0.13.1`（rust） — Copyright (c) 2014-2022 Steven Fackler, Yuki Okushi
- `phf_shared@0.13.1`（rust） — Copyright (c) 2014-2022 Steven Fackler, Yuki Okushi
- `plist@1.10.0`（rust） — Copyright (c) 2015 Edward Barnard
- `precomputed-hash@0.1.1`（rust） — Copyright (c) 2017 Emilio Cobos Álvarez
- `quick-xml@0.41.0`（rust） — Copyright (c) 2016 Johann Tuffe
- `redox_syscall@0.5.18`（rust） — 版权行见上游：https://gitlab.redox-os.org/redox-os/syscall
- `redox_users@0.5.2`（rust） — 版权行见上游：https://gitlab.redox-os.org/redox-os/users
- `schannel@0.1.29`（rust） — Copyright (c) 2015 steffengy
- `schemars@0.8.22`（rust） — Copyright (c) 2019 Graham Esau
- `schemars@0.9.0`（rust） — Copyright (c) 2019 Graham Esau
- `schemars@1.2.2`（rust） — Copyright (c) 2019 Graham Esau
- `schemars_derive@0.8.22`（rust） — Copyright (c) 2019 Graham Esau
- `shared_child@1.1.1`（rust） — 版权行见上游：https://github.com/oconnor663/shared_child.rs
- `sigchld@0.2.4`（rust） — 版权行见上游：https://github.com/oconnor663/sigchld.rs
- `simd-adler32@0.3.10`（rust） — 版权行见上游：https://github.com/mcountryman/simd-adler32
- `slab@0.4.12`（rust） — Copyright (c) 2019 Carl Lerche
- `soup3@0.5.0`（rust） — Copyright (c) 2013-2017, The Gtk-rs Project Developers.
- `soup3-sys@0.5.0`（rust） — Copyright (c) 2013-2017, The Gtk-rs Project Developers.
- `strsim@0.11.1`（rust） — Copyright (c) 2015 Danny Guo；Copyright (c) 2016 Titus Wormer <tituswormer@gmail.com>
- `synstructure@0.13.2`（rust） — Copyright 2016 Nika Layzell
- `tauri-winres@0.3.6`（rust） — Copyright (c) 2023 - Present Tauri Apps Contributors；Copyright (c) 2016 Max Resch
- `tokio@1.53.1`（rust） — 版权行见上游：https://github.com/tokio-rs/tokio
- `tokio-util@0.7.19`（rust） — 版权行见上游：https://github.com/tokio-rs/tokio
- `tower@0.5.3`（rust） — Copyright (c) 2019 Tower Contributors
- `tower-http@0.6.11`（rust） — Copyright (c) 2019-2021 Tower Contributors
- `tower-layer@0.3.3`（rust） — Copyright (c) 2019 Tower Contributors
- `tower-service@0.3.3`（rust） — Copyright (c) 2019 Tower Contributors
- `tracing@0.1.44`（rust） — Copyright (c) 2019 Tokio Contributors
- `tracing-core@0.1.36`（rust） — Copyright (c) 2019 Tokio Contributors
- `try-lock@0.2.5`（rust） — Copyright (c) 2018-2023 Sean McArthur；Copyright (c) 2016 Alex Crichton
- `urlpattern@0.3.0`（rust） — Copyright (c) 2021 the Deno authors
- `version-compare@0.2.1`（rust） — Copyright (c) 2017 Tim Visée
- `vswhom@0.1.0`（rust） — Copyright (c) 2019 nabijaczleweli
- `vswhom-sys@0.1.3`（rust） — Copyright (c) 2019 nabijaczleweli
- `want@0.3.1`（rust） — Copyright (c) 2018-2019 Sean McArthur
- `webkit2gtk@2.0.2`（rust） — Copyright (c) 2016 Boucher, Antoni <bouanto@zoho.com>；Copyright (c) 2017-2021, The Gtk-rs Project Developers.
- `webkit2gtk-sys@2.0.2`（rust） — Copyright (c) 2016 Boucher, Antoni <bouanto@zoho.com>
- `webview2-com@0.38.2`（rust） — 版权行见上游：https://github.com/wravery/webview2-rs
- `webview2-com-macros@0.8.1`（rust） — 版权行见上游：https://github.com/wravery/webview2-rs
- `webview2-com-sys@0.38.2`（rust） — 版权行见上游：https://github.com/wravery/webview2-rs
- `winnow@0.5.40`（rust） — 版权行见上游：https://github.com/winnow-rs/winnow
- `winnow@0.7.15`（rust） — 版权行见上游：https://github.com/winnow-rs/winnow
- `winnow@1.0.4`（rust） — 版权行见上游：https://github.com/winnow-rs/winnow
- `winreg@0.55.0`（rust） — Copyright (c) 2015 Igor Shaula
- `x11@2.21.0`（rust） — 版权行见上游：https://github.com/AltF02/x11-rs.git
- `x11-dl@2.21.0`（rust） — 版权行见上游：https://github.com/AltF02/x11-rs.git
- `zip@4.6.1`（rust） — Copyright (c) 2014 Mathijs van de Nes
- `zmij@1.0.23`（rust） — 版权行见上游：https://github.com/dtolnay/zmij

### MIT OR Apache-2.0（244 个）

- `@tauri-apps/plugin-process@2.3.1`（npm） — 版权行见上游：https://github.com/tauri-apps/plugins-workspace
- `@tauri-apps/plugin-updater@2.12.0`（npm） — 版权行见上游：https://github.com/tauri-apps/plugins-workspace
- `sniffio@1.3.1`（python） — 版权行见上游：Nathaniel J. Smith <njs@pobox.com> / https://github.com/python-trio/sniffio
- `android_log-sys@0.3.2`（rust） — 版权行见上游：https://github.com/rust-mobile/android_log-sys-rs
- `android_logger@0.15.1`（rust） — 版权行见上游：https://github.com/rust-mobile/android_logger-rs
- `android_system_properties@0.1.5`（rust） — 版权行见上游：https://github.com/nical/android_system_properties
- `anyhow@1.0.104`（rust） — 版权行见上游：https://github.com/dtolnay/anyhow
- `arbitrary@1.4.2`（rust） — 版权行见上游：https://github.com/rust-fuzz/arbitrary/
- `base64@0.21.7`（rust） — 版权行见上游：https://github.com/marshallpierce/rust-base64
- `base64@0.22.1`（rust） — Copyright (c) 2015 Alice Maz
- `bitflags@2.13.1`（rust） — Copyright (c) 2014 The Rust Project Developers
- `block-buffer@0.10.4`（rust） — 版权行见上游：https://github.com/RustCrypto/utils
- `bumpalo@3.20.3`（rust） — 版权行见上游：https://github.com/fitzgen/bumpalo
- `camino@1.2.5`（rust） — 版权行见上游：https://github.com/camino-rs/camino
- `cargo-platform@0.1.9`（rust） — 版权行见上游：https://github.com/rust-lang/cargo
- `cc@1.4.0`（rust） — Copyright (c) 2014 Alex Crichton
- `cfg-expr@0.15.8`（rust） — Copyright (c) 2019 Embark Studios
- `cfg-if@1.0.4`（rust） — Copyright (c) 2014 Alex Crichton
- `chrono@0.4.45`（rust） — Copyright (c) 2014, Kang Seonghoon.
- `cookie@0.18.1`（rust） — Copyright 2017 Sergio Benitez；Copyright 2014 Alex Chricton
- `core-foundation@0.10.1`（rust） — 版权行见上游：https://github.com/servo/core-foundation-rs
- `core-foundation@0.9.4`（rust） — 版权行见上游：https://github.com/servo/core-foundation-rs
- `core-foundation-sys@0.8.7`（rust） — 版权行见上游：https://github.com/servo/core-foundation-rs
- `core-graphics@0.25.0`（rust） — 版权行见上游：https://github.com/servo/core-foundation-rs
- `core-graphics-types@0.2.0`（rust） — 版权行见上游：https://github.com/servo/core-foundation-rs
- `cpufeatures@0.2.17`（rust） — 版权行见上游：https://github.com/RustCrypto/utils
- `crc32fast@1.5.0`（rust） — Copyright (c) 2018 Sam Rijs, Alex Crichton and contributors
- `crossbeam-channel@0.5.16`（rust） — Copyright (c) 2019 The Crossbeam Project Developers；Copyright (c) 2009 The Go Authors. All rights reserved.
- `crossbeam-utils@0.8.22`（rust） — Copyright (c) 2019 The Crossbeam Project Developers
- `crypto-common@0.1.7`（rust） — 版权行见上游：https://github.com/RustCrypto/traits
- `deranged@0.5.8`（rust） — Copyright 2024 Jacob Pratt et al.；Copyright (c) 2024 Jacob Pratt et al.
- `derive_arbitrary@1.4.2`（rust） — 版权行见上游：https://github.com/rust-fuzz/arbitrary
- `digest@0.10.7`（rust） — Copyright (c) 2017 Artyom Pavlov
- `dirs@6.0.0`（rust） — Copyright (c) 2018-2019 dirs-rs contributors
- `dirs-sys@0.5.0`（rust） — Copyright (c) 2018-2019 dirs-rs contributors
- `displaydoc@0.2.7`（rust） — 版权行见上游：https://github.com/yaahc/displaydoc
- `dtoa@1.0.11`（rust） — 版权行见上游：https://github.com/dtolnay/dtoa
- `dyn-clone@1.0.20`（rust） — 版权行见上游：https://github.com/dtolnay/dyn-clone
- `embed_plist@1.2.2`（rust） — 版权行见上游：https://github.com/nvzqz/embed-plist-rs
- `env_filter@0.1.4`（rust） — 版权行见上游：https://github.com/rust-cli/env_logger
- `erased-serde@0.4.10`（rust） — 版权行见上游：https://github.com/dtolnay/erased-serde
- `errno@0.3.14`（rust） — Copyright (c) 2014 Chris Wong
- `fdeflate@0.3.7`（rust） — 版权行见上游：https://github.com/image-rs/fdeflate
- `field-offset@0.3.6`（rust） — Copyright (c) 2016-2021 Diggory Blake, and other contributors.
- `find-msvc-tools@0.1.9`（rust） — Copyright (c) 2014 Alex Crichton
- `flate2@1.1.9`（rust） — Copyright (c) 2014-2026 Alex Crichton
- `foreign-types@0.5.0`（rust） — 版权行见上游：https://github.com/sfackler/foreign-types
- `foreign-types-macros@0.2.4`（rust） — 版权行见上游：https://github.com/sfackler/foreign-types
- `foreign-types-shared@0.3.1`（rust） — 版权行见上游：https://github.com/sfackler/foreign-types
- `form_urlencoded@1.2.2`（rust） — Copyright (c) 2013-2016 The rust-url developers
- `futures-channel@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `futures-core@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `futures-executor@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `futures-io@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `futures-macro@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `futures-sink@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `futures-task@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `futures-util@0.3.33`（rust） — Copyright (c) 2016 Alex Crichton；Copyright (c) 2017 The Tokio Authors
- `getrandom@0.2.17`（rust） — Copyright (c) 2018-2024 The rust-random Project Developers；Copyright (c) 2014 The Rust Project Developers
- `getrandom@0.3.4`（rust） — Copyright (c) 2018-2025 The rust-random Project Developers；Copyright (c) 2014 The Rust Project Developers
- `getrandom@0.4.3`（rust） — Copyright (c) 2018-2026 The rust-random Project Developers；Copyright (c) 2014 The Rust Project Developers
- `glob@0.3.4`（rust） — Copyright (c) 2014 The Rust Project Developers
- `hashbrown@0.12.3`（rust） — Copyright (c) 2016 Amanieu d'Antras
- `hashbrown@0.17.1`（rust） — Copyright (c) 2016 Amanieu d'Antras
- `heck@0.4.1`（rust） — Copyright (c) 2015 The Rust Project Developers
- `heck@0.5.0`（rust） — Copyright (c) 2015 The Rust Project Developers
- `hex@0.4.3`（rust） — Copyright (c) 2013-2014 The Rust Project Developers.；Copyright (c) 2015-2020 The rust-hex Developers
- `html5ever@0.38.0`（rust） — Copyright (c) 2014 The html5ever Project Developers
- `http@1.5.0`（rust） — Copyright 2017 http-rs authors；Copyright (c) 2017 http-rs authors
- `httparse@1.10.1`（rust） — Copyright (c) 2015-2025 Sean McArthur
- `iana-time-zone@0.1.65`（rust） — Copyright 2020 Andrew Straw；Copyright (c) 2020 Andrew D. Straw
- `iana-time-zone-haiku@0.1.2`（rust） — 版权行见上游：https://github.com/strawlab/iana-time-zone
- `idna@1.1.0`（rust） — Copyright (c) 2013-2025 The rust-url developers
- `ipnet@2.12.1`（rust） — Copyright 2017 Juniper Networks, Inc.
- `itoa@1.0.18`（rust） — 版权行见上游：https://github.com/dtolnay/itoa
- `jni@0.21.1`（rust） — 版权行见上游：https://github.com/jni-rs/jni-rs
- `jni@0.22.4`（rust） — 版权行见上游：https://github.com/jni-rs/jni-rs
- `jni-macros@0.22.4`（rust） — 版权行见上游：https://github.com/jni-rs/jni-rs
- `jni-sys@0.3.1`（rust） — 版权行见上游：https://github.com/jni-rs/jni-sys
- `jni-sys@0.4.1`（rust） — 版权行见上游：https://github.com/jni-rs/jni-sys
- `jni-sys-macros@0.4.1`（rust） — 版权行见上游：https://github.com/jni-rs/jni-sys
- `js-sys@0.3.103`（rust） — 版权行见上游：https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/js-sys
- `jsonptr@0.6.3`（rust） — Copyright 2024 Chance Dinkins；Copyright (c) 2022 Chance Dinkins
- `keyboard-types@0.7.0`（rust） — Copyright (c) 2017 Pyfisch
- `libc@0.2.189`（rust） — 版权行见上游：https://github.com/rust-lang/libc
- `lock_api@0.4.14`（rust） — Copyright (c) 2016 The Rust Project Developers
- `log@0.4.33`（rust） — Copyright (c) 2014 The Rust Project Developers
- `markup5ever@0.38.0`（rust） — Copyright (c) 2014 The html5ever Project Developers
- `mime@0.3.17`（rust） — Copyright (c) 2014 Sean McArthur
- `ndk@0.9.0`（rust） — 版权行见上游：https://github.com/rust-mobile/ndk
- `ndk-sys@0.6.0+11769913`（rust） — 版权行见上游：https://github.com/rust-mobile/ndk
- `num-conv@0.2.2`（rust） — 版权行见上游：https://github.com/jhpratt/num-conv
- `num-traits@0.2.19`（rust） — Copyright (c) 2014 The Rust Project Developers
- `num_threads@0.1.7`（rust） — Copyright 2021 Jacob Pratt；Copyright (c) 2021 Jacob Pratt
- `once_cell@1.21.4`（rust） — 版权行见上游：https://github.com/matklad/once_cell
- `openssl-probe@0.2.1`（rust） — Copyright (c) 2014 Alex Crichton
- `osakit@0.3.1`（rust） — 版权行见上游：https://github.com/mdevils/rust-osakit
- `parking_lot@0.12.5`（rust） — Copyright (c) 2016 The Rust Project Developers
- `parking_lot_core@0.9.12`（rust） — Copyright (c) 2016 The Rust Project Developers
- `percent-encoding@2.3.2`（rust） — Copyright (c) 2013-2025 The rust-url developers
- `pkg-config@0.3.33`（rust） — Copyright (c) 2014 Alex Crichton
- `png@0.17.16`（rust） — Copyright (c) 2015 nwin
- `png@0.18.1`（rust） — Copyright (c) 2015 nwin
- `powerfmt@0.2.0`（rust） — Copyright 2023 Jacob Pratt et al.；Copyright (c) 2023 Jacob Pratt et al.
- `proc-macro-crate@1.3.1`（rust） — 版权行见上游：https://github.com/bkchr/proc-macro-crate
- `proc-macro-crate@2.0.2`（rust） — 版权行见上游：https://github.com/bkchr/proc-macro-crate
- `proc-macro-crate@3.5.0`（rust） — 版权行见上游：https://github.com/bkchr/proc-macro-crate
- `proc-macro-error@1.0.4`（rust） — Copyright 2019-2020 CreepySkeleton <creepy-skeleton@yandex.ru>；Copyright (c) 2019-2020 CreepySkeleton
- `proc-macro-error-attr@1.0.4`（rust） — Copyright 2019-2020 CreepySkeleton <creepy-skeleton@yandex.ru>；Copyright (c) 2019-2020 CreepySkeleton
- `proc-macro2@1.0.107`（rust） — 版权行见上游：https://github.com/dtolnay/proc-macro2
- `quote@1.0.47`（rust） — 版权行见上游：https://github.com/dtolnay/quote
- `ref-cast@1.0.26`（rust） — 版权行见上游：https://github.com/dtolnay/ref-cast
- `ref-cast-impl@1.0.26`（rust） — 版权行见上游：https://github.com/dtolnay/ref-cast
- `regex@1.13.1`（rust） — Copyright (c) 2014 The Rust Project Developers
- `regex-automata@0.4.16`（rust） — Copyright (c) 2014 The Rust Project Developers
- `regex-syntax@0.8.11`（rust） — Copyright (c) 2014 The Rust Project Developers
- `reqwest@0.13.4`（rust） — Copyright 2016 Sean McArthur；Copyright (c) 2016-2026 Sean McArthur
- `rustc_version@0.4.1`（rust） — Copyright (c) 2016 The Rust Project Developers
- `rustls-pki-types@1.15.1`（rust） — Copyright 2023 Dirkjan Ochtman；Copyright (c) 2023 Dirkjan Ochtman <dirkjan@ochtman.nl>
- `rustls-platform-verifier@0.7.0`（rust） — Copyright (c) 2022 1Password
- `rustls-platform-verifier-android@0.1.1`（rust） — 版权行见上游：https://github.com/rustls/rustls-platform-verifier
- `rustversion@1.0.23`（rust） — 版权行见上游：https://github.com/dtolnay/rustversion
- `scopeguard@1.2.0`（rust） — Copyright (c) 2016-2019 Ulrik Sverdrup "bluss" and scopeguard developers
- `security-framework@3.7.0`（rust） — 版权行见上游：https://github.com/kornelski/rust-security-framework
- `security-framework-sys@2.17.0`（rust） — 版权行见上游：https://github.com/kornelski/rust-security-framework
- `semver@1.0.28`（rust） — 版权行见上游：https://github.com/dtolnay/semver
- `serde@1.0.229`（rust） — 版权行见上游：https://github.com/serde-rs/serde
- `serde-untagged@0.1.9`（rust） — 版权行见上游：https://github.com/dtolnay/serde-untagged
- `serde_core@1.0.229`（rust） — 版权行见上游：https://github.com/serde-rs/serde
- `serde_derive@1.0.229`（rust） — 版权行见上游：https://github.com/serde-rs/serde
- `serde_derive_internals@0.29.1`（rust） — 版权行见上游：https://github.com/serde-rs/serde
- `serde_json@1.0.151`（rust） — 版权行见上游：https://github.com/serde-rs/json
- `serde_repr@0.1.21`（rust） — 版权行见上游：https://github.com/dtolnay/serde-repr
- `serde_spanned@0.6.9`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `serde_spanned@1.1.1`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `serde_with@3.21.0`（rust） — 版权行见上游：https://github.com/jonasbb/serde_with/
- `serde_with_macros@3.21.0`（rust） — 版权行见上游：https://github.com/jonasbb/serde_with/
- `serialize-to-javascript@0.1.2`（rust） — Copyright (c) 2021 Chip Reed
- `serialize-to-javascript-impl@0.1.2`（rust） — Copyright (c) 2021 Chip Reed
- `servo_arc@0.4.3`（rust） — 版权行见上游：https://github.com/servo/stylo
- `sha2@0.10.9`（rust） — Copyright (c) 2006-2009 Graydon Hoare；Copyright (c) 2009-2013 Mozilla Foundation
- `shlex@2.0.1`（rust） — Copyright 2015 Nicholas Allegra (comex).；Copyright (c) 2015 Nicholas Allegra (comex).
- `signal-hook-registry@1.4.8`（rust） — Copyright (c) 2017 tokio-jsonrpc developers
- `simdutf8@0.1.5`（rust） — 版权行见上游：https://github.com/rusticstuff/simdutf8
- `smallvec@1.15.2`（rust） — Copyright (c) 2018 The Servo Project Developers
- `socket2@0.6.5`（rust） — Copyright (c) 2014 Alex Crichton
- `softbuffer@0.4.8`（rust） — Copyright 2022 Kirill Chibisov
- `stable_deref_trait@1.2.1`（rust） — Copyright (c) 2017 Robert Grosse
- `string_cache@0.9.0`（rust） — Copyright (c) 2012-2013 Mozilla Foundation
- `string_cache_codegen@0.6.1`（rust） — Copyright (c) 2012-2013 Mozilla Foundation
- `swift-rs@1.0.7`（rust） — 版权行见上游：https://github.com/Brendonovich/swift-rs
- `syn@1.0.109`（rust） — 版权行见上游：https://github.com/dtolnay/syn
- `syn@2.0.119`（rust） — 版权行见上游：https://github.com/dtolnay/syn
- `syn@3.0.3`（rust） — 版权行见上游：https://github.com/dtolnay/syn
- `system-configuration@0.7.0`（rust） — 版权行见上游：https://github.com/mullvad/system-configuration-rs
- `system-configuration-sys@0.6.0`（rust） — 版权行见上游：https://github.com/mullvad/system-configuration-rs
- `system-deps@6.2.2`（rust） — 版权行见上游：https://github.com/gdesmott/system-deps
- `tao-macros@0.1.4`（rust） — 版权行见上游：https://github.com/tauri-apps/tao
- `tar@0.4.46`（rust） — 版权行见上游：https://github.com/composefs/tar-rs
- `tempfile@3.27.0`（rust） — Copyright (c) 2015 Steven Allen
- `tendril@0.5.1`（rust） — Copyright (c) 2015 Keegan McAllister
- `thiserror@1.0.69`（rust） — 版权行见上游：https://github.com/dtolnay/thiserror
- `thiserror@2.0.19`（rust） — 版权行见上游：https://github.com/dtolnay/thiserror
- `thiserror-impl@1.0.69`（rust） — 版权行见上游：https://github.com/dtolnay/thiserror
- `thiserror-impl@2.0.19`（rust） — 版权行见上游：https://github.com/dtolnay/thiserror
- `time@0.3.55`（rust） — 版权行见上游：https://github.com/time-rs/time
- `time-core@0.1.9`（rust） — 版权行见上游：https://github.com/time-rs/time
- `time-macros@0.2.32`（rust） — 版权行见上游：https://github.com/time-rs/time
- `tokio-rustls@0.26.5`（rust） — Copyright 2017 quininer kel；Copyright (c) 2017 quininer kel
- `toml@0.8.2`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml@0.9.12+spec-1.1.0`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml@1.1.4+spec-1.1.0`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml_datetime@0.6.3`（rust） — Copyright (c) 2014 Alex Crichton
- `toml_datetime@0.7.5+spec-1.1.0`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml_datetime@1.1.1+spec-1.1.0`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml_edit@0.19.15`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml_edit@0.20.2`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml_edit@0.25.13+spec-1.1.0`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml_parser@1.1.3+spec-1.1.0`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `toml_writer@1.1.2+spec-1.1.0`（rust） — 版权行见上游：https://github.com/toml-rs/toml
- `tray-icon@0.24.2`（rust） — 版权行见上游：https://github.com/tauri-apps/tray-icon
- `typeid@1.0.3`（rust） — 版权行见上游：https://github.com/dtolnay/typeid
- `typenum@1.20.1`（rust） — Copyright 2014 Paho Lurie-Gregg；Copyright (c) 2014 Paho Lurie-Gregg
- `unicode-segmentation@1.13.3`（rust） — Copyright (c) 2015 The Rust Project Developers
- `url@2.5.8`（rust） — Copyright (c) 2013-2025 The rust-url developers
- `wasm-bindgen@0.2.126`（rust） — 版权行见上游：https://github.com/wasm-bindgen/wasm-bindgen
- `wasm-bindgen-futures@0.4.76`（rust） — 版权行见上游：https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/futures
- `wasm-bindgen-macro@0.2.126`（rust） — 版权行见上游：https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro
- `wasm-bindgen-macro-support@0.2.126`（rust） — 版权行见上游：https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro-support
- `wasm-bindgen-shared@0.2.126`（rust） — 版权行见上游：https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/shared
- `wasm-streams@0.5.0`（rust） — 版权行见上游：https://github.com/MattiasBuelens/wasm-streams/
- `web-sys@0.3.103`（rust） — 版权行见上游：https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/web-sys
- `web_atoms@0.2.5`（rust） — Copyright (c) 2014 The html5ever Project Developers
- `winapi-i686-pc-windows-gnu@0.4.0`（rust） — 版权行见上游：https://github.com/retep998/winapi-rs
- `winapi-x86_64-pc-windows-gnu@0.4.0`（rust） — 版权行见上游：https://github.com/retep998/winapi-rs
- `windows@0.61.3`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-collections@0.2.0`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-core@0.61.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-core@0.62.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-future@0.2.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-implement@0.60.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-interface@0.59.3`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-link@0.1.3`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-link@0.2.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-numerics@0.2.0`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-registry@0.6.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-result@0.3.4`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-result@0.4.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-strings@0.4.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-strings@0.5.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-sys@0.45.0`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-sys@0.52.0`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-sys@0.59.0`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-sys@0.60.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-sys@0.61.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-targets@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-targets@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-targets@0.53.5`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-threading@0.1.0`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows-version@0.1.7`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_aarch64_gnullvm@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_aarch64_gnullvm@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_aarch64_gnullvm@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_aarch64_msvc@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_aarch64_msvc@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_aarch64_msvc@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_gnu@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_gnu@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_gnu@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_gnullvm@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_gnullvm@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_msvc@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_msvc@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_i686_msvc@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_gnu@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_gnu@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_gnu@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_gnullvm@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_gnullvm@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_gnullvm@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_msvc@0.42.2`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_msvc@0.52.6`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `windows_x86_64_msvc@0.53.1`（rust） — 版权行见上游：https://github.com/microsoft/windows-rs
- `xattr@1.6.1`（rust） — Copyright (c) 2015 Steven Allen

### MIT OR Apache-2.0 OR LGPL-2.1-or-later（2 个）

- `r-efi@5.3.0`（rust） — 版权行见上游：https://github.com/r-efi/r-efi
- `r-efi@6.0.0`（rust） — 版权行见上游：https://github.com/r-efi/r-efi

### MIT OR Apache-2.0 OR Zlib（2 个）

- `raw-window-handle@0.6.2`（rust） — Copyright (c) 2019 Osspial；Copyright (c) 2020 Osspial
- `tinyvec_macros@0.1.1`（rust） — Copyright 2020 Tomasz "Soveu" Marx；Copyright (c) 2020 Soveu

### MIT OR Zlib OR Apache-2.0（1 个）

- `miniz_oxide@0.8.9`（rust） — Copyright 2013-2014 RAD Game Tools and Valve Software；Copyright 2010-2014 Rich Geldreich and Tenacious Software LLC

### MIT-0（3 个）

- `@csstools/color-helpers@6.1.0`（npm） — 版权行见上游：https://github.com/csstools/postcss-plugins
- `@csstools/css-syntax-patches-for-csstree@1.1.7`（npm） — 版权行见上游：https://github.com/csstools/postcss-plugins
- `cffi@2.1.1`（python） — 版权行见上游：Armin Rigo, Maciej Fijalkowski

### MIT/Apache-2.0（13 个）

- `bitflags@1.3.2`（rust） — Copyright (c) 2014 The Rust Project Developers
- `bs58@0.5.1`（rust） — Copyright (c) 2016 The roaring-rs developers.
- `filetime@0.2.29`（rust） — Copyright (c) 2014 Alex Crichton
- `ident_case@1.0.1`（rust） — 版权行见上游：https://github.com/TedDriggs/ident_case
- `json-patch@3.0.1`（rust） — Copyright (c) 2017 Ivan Dubrov
- `siphasher@1.0.3`（rust） — Copyright 2012-2016 The Rust Project Developers.；Copyright 2016-2026 Frank Denis.
- `unic-char-property@0.9.0`（rust） — 版权行见上游：https://github.com/open-i18n/rust-unic/
- `unic-char-range@0.9.0`（rust） — 版权行见上游：https://github.com/open-i18n/rust-unic/
- `unic-common@0.9.0`（rust） — 版权行见上游：https://github.com/open-i18n/rust-unic/
- `unic-ucd-ident@0.9.0`（rust） — 版权行见上游：https://github.com/open-i18n/rust-unic/
- `unic-ucd-version@0.9.0`（rust） — 版权行见上游：https://github.com/open-i18n/rust-unic/
- `version_check@0.9.5`（rust） — Copyright (c) 2017-2018 Sergio Benitez
- `winapi@0.3.9`（rust） — Copyright (c) 2015-2018 The winapi-rs Developers

### MPL-2.0（7 个）

- `python-build-standalone@astral-sh`（bundled） — Copyright (c) Astral Software Inc. — 上面那份 CPython 运行时的发行来源（scripts/build_runtime.ps1 通过 uv 取得）。
- `certifi@2026.7.22`（python） — 版权行见上游：Kenneth Reitz
- `cssparser@0.36.0`（rust） — 版权行见上游：https://github.com/servo/rust-cssparser
- `cssparser-macros@0.6.1`（rust） — 版权行见上游：https://github.com/servo/rust-cssparser
- `dtoa-short@0.3.5`（rust） — 版权行见上游：https://github.com/upsuper/dtoa-short
- `option-ext@0.2.0`（rust） — 版权行见上游：https://github.com/soc/option-ext.git
- `selectors@0.36.1`（rust） — 版权行见上游：https://github.com/servo/stylo

### MPL-2.0 AND MIT（1 个）

- `tqdm@4.70.1`（python） — Copyright (c) 2013 noamraph

### OFL-1.1（2 个）

- `Noto Serif SC@font-files`（bundled） — Google Inc. — 中文衬线字体，经 @fontsource/noto-serif-sc 分发，字体文件随前端产物分发。
- `@fontsource/noto-serif-sc@5.3.0`（npm） — 版权行见上游：Google Inc. / https://github.com/fontsource/font-files

### PSF-2.0（2 个）

- `CPython@3.11`（bundled） — Copyright (c) 2001-2026 Python Software Foundation. All Rights Reserved. — 安装包自带的 Python 运行时（resources/python-runtime，构建期由 scripts/build_runtime.ps1 生成），供「带第三方依赖的生成工具」离线建 venv；许可见该目录的 LICENSE-CPython.txt。
- `typing-extensions@4.16.0`（python） — Copyright (c) 1991 - 1995, Stichting Mathematisch Centrum Amsterdam,

### Unicode-3.0（18 个）

- `icu_collections@2.2.0`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `icu_locale_core@2.2.0`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `icu_normalizer@2.2.0`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `icu_normalizer_data@2.2.0`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `icu_properties@2.2.0`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `icu_properties_data@2.2.0`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `icu_provider@2.2.0`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `litemap@0.8.2`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `potential_utf@0.1.5`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `tinystr@0.8.3`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `writeable@0.6.3`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `yoke@0.8.3`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `yoke-derive@0.8.2`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `zerofrom@0.1.8`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `zerofrom-derive@0.1.7`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `zerotrie@0.2.4`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `zerovec@0.11.6`（rust） — Copyright © 2020-2024 Unicode, Inc.
- `zerovec-derive@0.11.3`（rust） — Copyright © 2020-2024 Unicode, Inc.

### Unlicense OR MIT（4 个）

- `aho-corasick@1.1.4`（rust） — Copyright (c) 2015 Andrew Gallant
- `byteorder@1.5.0`（rust） — Copyright (c) 2015 Andrew Gallant
- `memchr@2.8.3`（rust） — Copyright (c) 2015 Andrew Gallant
- `winapi-util@0.1.11`（rust） — Copyright (c) 2017 Andrew Gallant

### Unlicense/MIT（2 个）

- `same-file@1.0.6`（rust） — Copyright (c) 2017 Andrew Gallant
- `walkdir@2.5.0`（rust） — Copyright (c) 2015 Andrew Gallant

### Zlib（1 个）

- `foldhash@0.2.0`（rust） — Copyright (c) 2024 Orson Peters

### Zlib OR Apache-2.0 OR MIT（18 个）

- `bytemuck@1.25.2`（rust） — 版权行见上游：https://github.com/Lokathor/bytemuck
- `dispatch2@0.3.1`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-app-kit@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-cloud-kit@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-core-data@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-core-foundation@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-core-graphics@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-core-image@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-core-location@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-core-text@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-exception-helper@0.1.1`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-io-surface@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-osa-kit@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-quartz-core@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-ui-kit@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-user-notifications@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `objc2-web-kit@0.3.2`（rust） — 版权行见上游：https://github.com/madsmtm/objc2
- `tinyvec@1.12.0`（rust） — Copyright (c) 2019 Daniel "Lokathor" Gee.

## 上游未提供版权行的组件

这些上游包没有可提取的版权行（其许可证正文也不含版权行）：

- `@csstools/color-helpers@6.1.0`（npm，MIT-0）— https://github.com/csstools/postcss-plugins
- `@csstools/css-calc@3.3.0`（npm，MIT）— https://github.com/csstools/postcss-plugins
- `@csstools/css-color-parser@4.1.10`（npm，MIT）— https://github.com/csstools/postcss-plugins
- `@csstools/css-parser-algorithms@4.0.0`（npm，MIT）— https://github.com/csstools/postcss-plugins
- `@csstools/css-syntax-patches-for-csstree@1.1.7`（npm，MIT-0）— https://github.com/csstools/postcss-plugins
- `@csstools/css-tokenizer@4.0.0`（npm，MIT）— https://github.com/csstools/postcss-plugins
- `@esbuild/aix-ppc64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/aix-ppc64
- `@esbuild/android-arm@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/android-arm
- `@esbuild/android-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/android-arm64
- `@esbuild/android-x64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/android-x64
- `@esbuild/darwin-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/darwin-arm64
- `@esbuild/darwin-x64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/darwin-x64
- `@esbuild/freebsd-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/freebsd-arm64
- `@esbuild/freebsd-x64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/freebsd-x64
- `@esbuild/linux-arm@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-arm
- `@esbuild/linux-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-arm64
- `@esbuild/linux-ia32@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-ia32
- `@esbuild/linux-loong64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-loong64
- `@esbuild/linux-mips64el@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-mips64el
- `@esbuild/linux-ppc64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-ppc64
- `@esbuild/linux-riscv64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-riscv64
- `@esbuild/linux-s390x@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-s390x
- `@esbuild/linux-x64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/linux-x64
- `@esbuild/netbsd-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/netbsd-arm64
- `@esbuild/netbsd-x64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/netbsd-x64
- `@esbuild/openbsd-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/openbsd-arm64
- `@esbuild/openbsd-x64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/openbsd-x64
- `@esbuild/openharmony-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/openharmony-arm64
- `@esbuild/sunos-x64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/sunos-x64
- `@esbuild/win32-arm64@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/win32-arm64
- `@esbuild/win32-ia32@0.25.12`（npm，MIT）— https://www.npmjs.com/package/@esbuild/win32-ia32
- `@esbuild/win32-x64@0.25.12`（npm，MIT）— https://github.com/evanw/esbuild
- `@fontsource/noto-serif-sc@5.3.0`（npm，OFL-1.1）— Google Inc. / https://github.com/fontsource/font-files
- `@jridgewell/sourcemap-codec@1.5.5`（npm，MIT）— Justin Ridgewell <justin@ridgewell.name> / https://github.com/jridgewell/sourcemaps
- `@napi-rs/canvas-android-arm64@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-android-arm64
- `@napi-rs/canvas-darwin-arm64@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-darwin-arm64
- `@napi-rs/canvas-darwin-x64@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-darwin-x64
- `@napi-rs/canvas-linux-arm-gnueabihf@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-linux-arm-gnueabihf
- `@napi-rs/canvas-linux-arm64-gnu@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-linux-arm64-gnu
- `@napi-rs/canvas-linux-arm64-musl@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-linux-arm64-musl
- `@napi-rs/canvas-linux-riscv64-gnu@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-linux-riscv64-gnu
- `@napi-rs/canvas-linux-x64-gnu@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-linux-x64-gnu
- `@napi-rs/canvas-linux-x64-musl@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-linux-x64-musl
- `@napi-rs/canvas-win32-arm64-msvc@1.0.11`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/canvas-win32-arm64-msvc
- `@napi-rs/canvas-win32-x64-msvc@1.0.11`（npm，MIT）— https://github.com/Brooooooklyn/canvas
- `@napi-rs/lzma-linux-x64-gnu@1.5.1`（npm，MIT）— https://www.npmjs.com/package/@napi-rs/lzma-linux-x64-gnu
- `@pkgjs/parseargs@0.11.0`（npm，MIT）— git@github.com:pkgjs/parseargs
- `@rollup/rollup-android-arm-eabi@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-android-arm-eabi
- `@rollup/rollup-android-arm64@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-android-arm64
- `@rollup/rollup-darwin-arm64@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-darwin-arm64
- `@rollup/rollup-darwin-x64@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-darwin-x64
- `@rollup/rollup-freebsd-arm64@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-freebsd-arm64
- `@rollup/rollup-freebsd-x64@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-freebsd-x64
- `@rollup/rollup-linux-arm-gnueabihf@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-arm-gnueabihf
- `@rollup/rollup-linux-arm-musleabihf@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-arm-musleabihf
- `@rollup/rollup-linux-arm64-gnu@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-arm64-gnu
- `@rollup/rollup-linux-arm64-musl@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-arm64-musl
- `@rollup/rollup-linux-loong64-gnu@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-loong64-gnu
- `@rollup/rollup-linux-loong64-musl@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-loong64-musl
- `@rollup/rollup-linux-ppc64-gnu@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-ppc64-gnu
- `@rollup/rollup-linux-ppc64-musl@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-ppc64-musl
- `@rollup/rollup-linux-riscv64-gnu@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-riscv64-gnu
- `@rollup/rollup-linux-riscv64-musl@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-riscv64-musl
- `@rollup/rollup-linux-s390x-gnu@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-s390x-gnu
- `@rollup/rollup-linux-x64-gnu@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-x64-gnu
- `@rollup/rollup-linux-x64-musl@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-linux-x64-musl
- `@rollup/rollup-openbsd-x64@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-openbsd-x64
- `@rollup/rollup-openharmony-arm64@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-openharmony-arm64
- `@rollup/rollup-win32-arm64-msvc@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-win32-arm64-msvc
- `@rollup/rollup-win32-ia32-msvc@4.62.4`（npm，MIT）— https://www.npmjs.com/package/@rollup/rollup-win32-ia32-msvc
- `@rollup/rollup-win32-x64-gnu@4.62.4`（npm，MIT）— Lukas Taegert-Atkinson / https://github.com/rollup/rollup
- `@rollup/rollup-win32-x64-msvc@4.62.4`（npm，MIT）— Lukas Taegert-Atkinson / https://github.com/rollup/rollup
- `@standard-schema/spec@1.1.0`（npm，MIT）— Colin McDonnell / https://github.com/standard-schema/standard-schema
- `@tauri-apps/cli-darwin-arm64@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-darwin-arm64
- `@tauri-apps/cli-darwin-x64@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-darwin-x64
- `@tauri-apps/cli-linux-arm-gnueabihf@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-linux-arm-gnueabihf
- `@tauri-apps/cli-linux-arm64-gnu@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-linux-arm64-gnu
- `@tauri-apps/cli-linux-arm64-musl@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-linux-arm64-musl
- `@tauri-apps/cli-linux-riscv64-gnu@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-linux-riscv64-gnu
- `@tauri-apps/cli-linux-x64-gnu@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-linux-x64-gnu
- `@tauri-apps/cli-linux-x64-musl@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-linux-x64-musl
- `@tauri-apps/cli-win32-arm64-msvc@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-win32-arm64-msvc
- `@tauri-apps/cli-win32-ia32-msvc@2.11.4`（npm，Apache-2.0 OR MIT）— https://www.npmjs.com/package/@tauri-apps/cli-win32-ia32-msvc
- `@tauri-apps/cli-win32-x64-msvc@2.11.4`（npm，Apache-2.0 OR MIT）— https://github.com/tauri-apps/tauri
- `@tauri-apps/plugin-process@2.3.1`（npm，MIT OR Apache-2.0）— https://github.com/tauri-apps/plugins-workspace
- `@tauri-apps/plugin-updater@2.12.0`（npm，MIT OR Apache-2.0）— https://github.com/tauri-apps/plugins-workspace
- `@types/chai@5.2.3`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/debug@4.1.13`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/deep-eql@4.0.2`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/estree@1.0.9`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/hast@3.0.5`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/mdast@4.0.4`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/ms@2.1.0`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/node@26.2.0`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/stats.js@0.17.4`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/three@0.185.3`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/unist@3.0.3`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/unist@2.0.11`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@types/webxr@0.5.24`（npm，MIT）— https://github.com/DefinitelyTyped/DefinitelyTyped
- `@vue/compiler-vue2@2.7.16`（npm，MIT）— Evan You / https://github.com/vuejs/vue
- `@vue/devtools-api@6.6.4`（npm，MIT）— Guillaume Chau / https://github.com/vuejs/vue-devtools
- `ansi-regex@6.2.2`（npm，MIT）— Sindre Sorhus / chalk/ansi-regex
- `ansi-regex@5.0.1`（npm，MIT）— Sindre Sorhus / chalk/ansi-regex
- `ansi-styles@6.2.3`（npm，MIT）— Sindre Sorhus / chalk/ansi-styles
- `ansi-styles@4.3.0`（npm，MIT）— Sindre Sorhus / chalk/ansi-styles
- `data-urls@7.0.0`（npm，MIT）— Domenic Denicola <d@domenic.me> (https://domenic.me/) / https://github.com/jsdom/data-urls
- `de-indent@1.0.2`（npm，MIT）— Evan You / https://github.com/yyx990803/de-indent
- `decode-named-character-reference@1.3.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/decode-named-character-reference
- `dequal@2.0.3`（npm，MIT）— Luke Edwards / lukeed/dequal
- `eastasianwidth@0.2.0`（npm，MIT）— Masaki Komagata / git://github.com/komagata/eastasianwidth
- `entities@7.0.1`（npm，BSD-2-Clause）— Felix Boehm <me@feedic.com> / https://github.com/fb55/entities
- `entities@8.0.0`（npm，BSD-2-Clause）— Felix Boehm <me@feedic.com> / https://github.com/fb55/entities
- `entities@6.0.1`（npm，BSD-2-Clause）— Felix Boehm <me@feedic.com> / git://github.com/fb55/entities
- `escape-string-regexp@5.0.0`（npm，MIT）— Sindre Sorhus / sindresorhus/escape-string-regexp
- `fsevents@2.3.3`（npm，MIT）— https://www.npmjs.com/package/fsevents
- `hast-util-from-parse5@8.0.3`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-from-parse5
- `hast-util-raw@9.1.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-raw
- `hast-util-to-html@9.0.5`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-to-html
- `hast-util-to-parse5@8.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hast-util-to-parse5
- `hastscript@9.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/hastscript
- `html-encoding-sniffer@6.0.0`（npm，MIT）— Domenic Denicola <d@domenic.me> (https://domenic.me/) / https://github.com/jsdom/html-encoding-sniffer
- `ini@1.3.8`（npm，ISC）— Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / git://github.com/isaacs/ini
- `is-fullwidth-code-point@3.0.0`（npm，MIT）— Sindre Sorhus / sindresorhus/is-fullwidth-code-point
- `is-plain-obj@4.1.0`（npm，MIT）— Sindre Sorhus / sindresorhus/is-plain-obj
- `isexe@2.0.0`（npm，ISC）— Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / https://github.com/isaacs/isexe
- `jackspeak@3.4.3`（npm，BlueOak-1.0.0）— Isaac Z. Schlueter <i@izs.me> / https://github.com/isaacs/jackspeak
- `lru-cache@11.5.2`（npm，BlueOak-1.0.0）— Isaac Z. Schlueter <i@izs.me> / ssh://git@github.com/isaacs/node-lru-cache
- `markdown-table@3.0.4`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/markdown-table
- `mdast-util-directive@3.1.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-directive
- `mdast-util-find-and-replace@3.0.2`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-find-and-replace
- `mdast-util-from-markdown@2.0.3`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-from-markdown
- `mdast-util-gfm@3.1.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-gfm
- `mdast-util-gfm-footnote@2.1.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-gfm-footnote
- `mdast-util-to-markdown@2.1.2`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / syntax-tree/mdast-util-to-markdown
- `mdn-data@2.27.1`（npm，CC0-1.0）— Mozilla Developer Network / https://github.com/mdn/data
- `micromark@4.0.2`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark
- `micromark-core-commonmark@2.0.3`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-core-commonmark
- `micromark-extension-directive@4.0.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / micromark/micromark-extension-directive
- `micromark-extension-gfm-table@2.1.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / micromark/micromark-extension-gfm-table
- `micromark-factory-destination@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-destination
- `micromark-factory-label@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-label
- `micromark-factory-space@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-space
- `micromark-factory-title@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-title
- `micromark-factory-whitespace@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-factory-whitespace
- `micromark-util-character@2.1.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-character
- `micromark-util-chunked@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-chunked
- `micromark-util-classify-character@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-classify-character
- `micromark-util-combine-extensions@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-combine-extensions
- `micromark-util-decode-numeric-character-reference@2.0.2`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-decode-numeric-character-reference
- `micromark-util-decode-string@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-decode-string
- `micromark-util-encode@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-encode
- `micromark-util-html-tag-name@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-html-tag-name
- `micromark-util-normalize-identifier@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-normalize-identifier
- `micromark-util-resolve-all@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-resolve-all
- `micromark-util-sanitize-uri@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-sanitize-uri
- `micromark-util-subtokenize@2.1.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-subtokenize
- `micromark-util-symbol@2.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-symbol
- `micromark-util-types@2.0.2`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / https://github.com/micromark/micromark/tree/main/packages/micromark-util-types
- `minipass@7.1.3`（npm，BlueOak-1.0.0）— Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / https://github.com/isaacs/minipass
- `nopt@7.2.1`（npm，ISC）— GitHub Inc. / https://github.com/npm/nopt
- `package-json-from-dist@1.0.1`（npm，BlueOak-1.0.0）— Isaac Z. Schlueter <i@izs.me> (https://izs.me) / https://github.com/isaacs/package-json-from-dist
- `parse-entities@4.0.2`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/parse-entities
- `parse5@8.0.1`（npm，MIT）— Ivan Nikulin <ifaaan@gmail.com> (https://github.com/inikulin) / git://github.com/inikulin/parse5
- `parse5@7.3.0`（npm，MIT）— Ivan Nikulin <ifaaan@gmail.com> (https://github.com/inikulin) / git://github.com/inikulin/parse5
- `path-key@3.1.1`（npm，MIT）— Sindre Sorhus / sindresorhus/path-key
- `path-scurry@1.11.1`（npm，BlueOak-1.0.0）— Isaac Z. Schlueter <i@izs.me> (https://blog.izs.me) / https://github.com/isaacs/path-scurry
- `pdfjs-dist@6.4.299`（npm，Apache-2.0）— https://github.com/mozilla/pdf.js
- `pinia@2.3.1`（npm，MIT）— Eduardo San Martin Morote / https://github.com/vuejs/pinia
- `property-information@7.2.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / wooorm/property-information
- `proto-list@1.2.4`（npm，ISC）— Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me/) / https://github.com/isaacs/proto-list
- `remark-directive@4.0.0`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / remarkjs/remark-directive
- `remark-gfm@4.0.1`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / remarkjs/remark-gfm
- `require-from-string@2.0.2`（npm，MIT）— Vsevolod Strukchinsky / floatdrop/require-from-string
- `saxes@6.0.0`（npm，ISC）— Louis-Dominique Dubeau <ldd@lddubeau.com> / https://github.com/lddubeau/saxes
- `semver@7.8.5`（npm，ISC）— GitHub Inc. / https://github.com/npm/node-semver
- `shebang-command@2.0.0`（npm，MIT）— Kevin Mårtensson / kevva/shebang-command
- `shebang-regex@3.0.0`（npm，MIT）— Sindre Sorhus / sindresorhus/shebang-regex
- `signal-exit@4.1.0`（npm，ISC）— Ben Coe <ben@npmjs.com> / https://github.com/tapjs/signal-exit
- `stackback@0.0.2`（npm，MIT）— Roman Shtylman <shtylman@gmail.com> / git://github.com/shtylman/node-stackback
- `std-env@4.2.0`（npm，MIT）— unjs/std-env
- `string-width@5.1.2`（npm，MIT）— Sindre Sorhus / sindresorhus/string-width
- `string-width@4.2.3`（npm，MIT）— Sindre Sorhus / sindresorhus/string-width
- `string-width-cjs@4.2.3`（npm，MIT）— Sindre Sorhus / sindresorhus/string-width
- `strip-ansi@7.2.0`（npm，MIT）— Sindre Sorhus / chalk/strip-ansi
- `strip-ansi@6.0.1`（npm，MIT）— Sindre Sorhus / chalk/strip-ansi
- `strip-ansi-cjs@6.0.1`（npm，MIT）— Sindre Sorhus / chalk/strip-ansi
- `tr46@6.0.0`（npm，MIT）— Sebastian Mayr <npm@smayr.name> / https://github.com/jsdom/tr46
- `typescript@5.9.3`（npm，Apache-2.0）— Microsoft Corp. / https://github.com/microsoft/TypeScript
- `undici@7.29.0`（npm，MIT）— https://github.com/nodejs/undici
- `undici-types@8.3.0`（npm，MIT）— https://github.com/nodejs/undici
- `vfile-message@4.0.3`（npm，MIT）— Titus Wormer <tituswormer@gmail.com> (https://wooorm.com) / vfile/vfile-message
- `vscode-uri@3.1.0`（npm，MIT）— Microsoft / https://github.com/microsoft/vscode-uri
- `vue-router@4.6.4`（npm，MIT）— Eduardo San Martin Morote / https://github.com/vuejs/router
- `w3c-xmlserializer@5.0.0`（npm，MIT）— jsdom/w3c-xmlserializer
- `whatwg-mimetype@5.0.0`（npm，MIT）— Domenic Denicola <d@domenic.me> (https://domenic.me/) / jsdom/whatwg-mimetype
- `whatwg-url@16.0.1`（npm，MIT）— Sebastian Mayr <github@smayr.name> / https://github.com/jsdom/whatwg-url
- `which@2.0.2`（npm，ISC）— Isaac Z. Schlueter <i@izs.me> (http://blog.izs.me) / git://github.com/isaacs/node-which
- `wrap-ansi@8.1.0`（npm，MIT）— Sindre Sorhus / chalk/wrap-ansi
- `wrap-ansi-cjs@7.0.0`（npm，MIT）— Sindre Sorhus / chalk/wrap-ansi
- `xml-name-validator@5.0.0`（npm，Apache-2.0）— Domenic Denicola <d@domenic.me> (https://domenic.me/) / jsdom/xml-name-validator
- `xmlchars@2.2.0`（npm，MIT）— Louis-Dominique Dubeau <ldd@lddubeau.com> / https://github.com/lddubeau/xmlchars
- `backports-tarfile@1.2.0`（python，MIT）— https://pypi.org/project/backports-tarfile/
- `beautifulsoup4@4.15.0`（python，MIT）— Leonard Richardson <leonardr@segfault.org> / https://www.crummy.com/software/BeautifulSoup/bs4/download/
- `certifi@2026.7.22`（python，MPL-2.0）— Kenneth Reitz
- `cffi@2.1.1`（python，MIT-0）— Armin Rigo, Maciej Fijalkowski
- `cryptography@50.0.1`（python，Apache-2.0 OR BSD-3-Clause）— https://pypi.org/project/cryptography/
- `flatbuffers@25.12.19`（python，Apache-2.0）— Derek Bailey
- `fsspec@2026.7.0`（python，BSD-3-Clause）— https://filesystem-spec.readthedocs.io/en/latest/changelog.html
- `hf-xet@1.6.0`（python，Apache-2.0）— https://huggingface.co/docs/hub/xet/index
- `huggingface-hub@1.31.0`（python，Apache-2.0）— Hugging Face, Inc.
- `importlib-metadata@9.0.1`（python，Apache-2.0）— Jason R. Coombs <jaraco@jaraco.com> / https://github.com/python/importlib_metadata
- `jaraco-classes@3.4.0`（python，MIT）— Jason R. Coombs
- `jaraco-context@6.1.2`（python，MIT）— Jason R. Coombs <jaraco@jaraco.com> / https://github.com/jaraco/jaraco.context
- `jaraco-functools@4.6.0`（python，MIT）— Jason R. Coombs <jaraco@jaraco.com> / https://github.com/jaraco/jaraco.functools
- `jeepney@0.9.0`（python，MIT）— https://pypi.org/project/jeepney/
- `jiter@0.16.0`（python，MIT）— Samuel Colvin <s@muelcolvin.com>
- `keyring@25.7.0`（python，MIT）— Kang Zhang <jobo.zh@gmail.com> / https://github.com/jaraco/keyring
- `onnxruntime@1.30.0`（python，MIT）— Microsoft Corporation
- `packaging@26.3`（python，Apache-2.0 OR BSD-2-Clause）— Donald Stufft <donald@stufft.io> / https://packaging.pypa.io/
- `pycparser@3.0`（python，BSD-3-Clause）— https://pypi.org/project/pycparser/
- `pydantic@2.13.5`（python，MIT）— Samuel Colvin <s@muelcolvin.com>, Eric Jolibois <em.jolibois@gmail.com>, Hasan Ramezani <hasan.r67@gmail.com>, Adrian Garcia Badaracco <1755071+adriangb@users.noreply.github.com>, Terrence Dorsey <terry@pydantic.dev>, David Montague <david@pydantic.dev>, Serge Matveenko <lig@countzero.co>, Marcelo Trylesinski <marcelotryle@gmail.com>, Sydney Runkle <sydneymarierunkle@gmail.com>, David Hewitt <mail@davidhewitt.dev>, Alex Hall <alex.mojaki@gmail.com>, Victorien Plot <contact@vctrn.dev> / https://github.com/pydantic/pydantic
- `pytest-asyncio@1.4.0`（python，Apache-2.0）— Tin Tvrtković <tinchester@gmail.com> / https://github.com/pytest-dev/pytest-asyncio/issues
- `secretstorage@3.5.0`（python，BSD-3-Clause）— https://pypi.org/project/secretstorage/
- `sniffio@1.3.1`（python，MIT OR Apache-2.0）— Nathaniel J. Smith <njs@pobox.com> / https://github.com/python-trio/sniffio
- `tokenizers@0.23.2`（python，Apache-2.0）— Nicolas Patry <patry.nicolas@protonmail.com>, Anthony Moi <anthony@huggingface.co> / https://github.com/huggingface/tokenizers
- `typing-inspection@0.4.4`（python，MIT）— Victorien Plot <contact@vctrn.dev> / https://github.com/pydantic/typing-inspection
- `uvloop@0.22.1`（python，MIT）— https://pypi.org/project/uvloop/
- `watchfiles@1.2.0`（python，MIT）— Samuel Colvin <s@muelcolvin.com> / https://github.com/samuelcolvin/watchfiles/releases
- `websockets@17.1`（python，BSD-3-Clause）— Aymeric Augustin <aymeric.augustin@m4x.org> / https://github.com/python-websockets/websockets
- `zipp@4.1.0`（python，MIT）— Jason R. Coombs <jaraco@jaraco.com> / https://github.com/jaraco/zipp
- `adler2@2.0.1`（rust，0BSD OR MIT OR Apache-2.0）— https://github.com/oyvindln/adler2
- `alloc-stdlib@0.2.4`（rust，BSD-3-Clause）— https://github.com/dropbox/rust-alloc-no-stdlib
- `android_log-sys@0.3.2`（rust，MIT OR Apache-2.0）— https://github.com/rust-mobile/android_log-sys-rs
- `android_logger@0.15.1`（rust，MIT OR Apache-2.0）— https://github.com/rust-mobile/android_logger-rs
- `android_system_properties@0.1.5`（rust，MIT OR Apache-2.0）— https://github.com/nical/android_system_properties
- `anyhow@1.0.104`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/anyhow
- `arbitrary@1.4.2`（rust，MIT OR Apache-2.0）— https://github.com/rust-fuzz/arbitrary/
- `atk@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `atk-sys@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `base64@0.21.7`（rust，MIT OR Apache-2.0）— https://github.com/marshallpierce/rust-base64
- `block-buffer@0.10.4`（rust，MIT OR Apache-2.0）— https://github.com/RustCrypto/utils
- `block2@0.6.2`（rust，MIT）— https://github.com/madsmtm/objc2
- `bumpalo@3.20.3`（rust，MIT OR Apache-2.0）— https://github.com/fitzgen/bumpalo
- `bytemuck@1.25.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/Lokathor/bytemuck
- `cairo-rs@0.18.5`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `cairo-sys-rs@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `camino@1.2.5`（rust，MIT OR Apache-2.0）— https://github.com/camino-rs/camino
- `cargo-platform@0.1.9`（rust，MIT OR Apache-2.0）— https://github.com/rust-lang/cargo
- `cargo_metadata@0.19.2`（rust，MIT）— https://github.com/oli-obk/cargo_metadata
- `cargo_toml@0.22.3`（rust，Apache-2.0 OR MIT）— https://gitlab.com/lib.rs/cargo_toml
- `cesu8@1.1.0`（rust，Apache-2.0 OR MIT）— https://github.com/emk/cesu8-rs
- `combine@4.6.7`（rust，MIT）— https://github.com/Marwes/combine
- `core-foundation@0.9.4`（rust，MIT OR Apache-2.0）— https://github.com/servo/core-foundation-rs
- `core-foundation@0.10.1`（rust，MIT OR Apache-2.0）— https://github.com/servo/core-foundation-rs
- `core-foundation-sys@0.8.7`（rust，MIT OR Apache-2.0）— https://github.com/servo/core-foundation-rs
- `core-graphics@0.25.0`（rust，MIT OR Apache-2.0）— https://github.com/servo/core-foundation-rs
- `core-graphics-types@0.2.0`（rust，MIT OR Apache-2.0）— https://github.com/servo/core-foundation-rs
- `cpufeatures@0.2.17`（rust，MIT OR Apache-2.0）— https://github.com/RustCrypto/utils
- `crypto-common@0.1.7`（rust，MIT OR Apache-2.0）— https://github.com/RustCrypto/traits
- `cssparser@0.36.0`（rust，MPL-2.0）— https://github.com/servo/rust-cssparser
- `cssparser-macros@0.6.1`（rust，MPL-2.0）— https://github.com/servo/rust-cssparser
- `ctor@0.8.0`（rust，Apache-2.0 OR MIT）— https://github.com/mmastrac/rust-ctor
- `ctor-proc-macro@0.0.7`（rust，Apache-2.0 OR MIT）— https://github.com/mmastrac/rust-ctor
- `derive_arbitrary@1.4.2`（rust，MIT OR Apache-2.0）— https://github.com/rust-fuzz/arbitrary
- `dispatch2@0.3.1`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `displaydoc@0.2.7`（rust，MIT OR Apache-2.0）— https://github.com/yaahc/displaydoc
- `dlopen2@0.8.2`（rust，MIT）— https://github.com/OpenByteDev/dlopen2
- `dlopen2_derive@0.4.3`（rust，MIT）— https://github.com/OpenByteDev/dlopen2
- `dtoa@1.0.11`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/dtoa
- `dtoa-short@0.3.5`（rust，MPL-2.0）— https://github.com/upsuper/dtoa-short
- `dtor@0.3.0`（rust，Apache-2.0 OR MIT）— https://github.com/mmastrac/rust-ctor
- `dtor-proc-macro@0.0.6`（rust，Apache-2.0 OR MIT）— https://github.com/mmastrac/rust-ctor
- `dunce@1.0.5`（rust，CC0-1.0 OR MIT-0 OR Apache-2.0）— https://gitlab.com/kornelski/dunce
- `dyn-clone@1.0.20`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/dyn-clone
- `embed_plist@1.2.2`（rust，MIT OR Apache-2.0）— https://github.com/nvzqz/embed-plist-rs
- `env_filter@0.1.4`（rust，MIT OR Apache-2.0）— https://github.com/rust-cli/env_logger
- `erased-serde@0.4.10`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/erased-serde
- `fastrand@2.5.0`（rust，Apache-2.0 OR MIT）— https://github.com/smol-rs/fastrand
- `fdeflate@0.3.7`（rust，MIT OR Apache-2.0）— https://github.com/image-rs/fdeflate
- `foreign-types@0.5.0`（rust，MIT OR Apache-2.0）— https://github.com/sfackler/foreign-types
- `foreign-types-macros@0.2.4`（rust，MIT OR Apache-2.0）— https://github.com/sfackler/foreign-types
- `foreign-types-shared@0.3.1`（rust，MIT OR Apache-2.0）— https://github.com/sfackler/foreign-types
- `gdk@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `gdk-pixbuf@0.18.5`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `gdk-pixbuf-sys@0.18.0`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `gdk-sys@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `gdkwayland-sys@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `gdkx11@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `gdkx11-sys@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `gio@0.18.4`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `gio-sys@0.18.1`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `glib@0.18.5`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `glib-macros@0.18.5`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `glib-sys@0.18.1`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `gobject-sys@0.18.0`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `gtk@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `gtk-sys@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `gtk3-macros@0.18.2`（rust，MIT）— https://github.com/gtk-rs/gtk3-rs
- `iana-time-zone-haiku@0.1.2`（rust，MIT OR Apache-2.0）— https://github.com/strawlab/iana-time-zone
- `ident_case@1.0.1`（rust，MIT/Apache-2.0）— https://github.com/TedDriggs/ident_case
- `idna_adapter@1.2.2`（rust，Apache-2.0 OR MIT）— https://github.com/hsivonen/idna_adapter
- `itoa@1.0.18`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/itoa
- `jni@0.21.1`（rust，MIT OR Apache-2.0）— https://github.com/jni-rs/jni-rs
- `jni@0.22.4`（rust，MIT OR Apache-2.0）— https://github.com/jni-rs/jni-rs
- `jni-macros@0.22.4`（rust，MIT OR Apache-2.0）— https://github.com/jni-rs/jni-rs
- `jni-sys@0.3.1`（rust，MIT OR Apache-2.0）— https://github.com/jni-rs/jni-sys
- `jni-sys@0.4.1`（rust，MIT OR Apache-2.0）— https://github.com/jni-rs/jni-sys
- `jni-sys-macros@0.4.1`（rust，MIT OR Apache-2.0）— https://github.com/jni-rs/jni-sys
- `js-sys@0.3.103`（rust，MIT OR Apache-2.0）— https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/js-sys
- `libappindicator-sys@0.9.0`（rust，Apache-2.0 OR MIT）— https://crates.io/crates/libappindicator-sys
- `libc@0.2.189`（rust，MIT OR Apache-2.0）— https://github.com/rust-lang/libc
- `libredox@0.1.18`（rust，MIT）— https://gitlab.redox-os.org/redox-os/libredox.git
- `linux-raw-sys@0.12.1`（rust，Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT）— https://github.com/sunfishcode/linux-raw-sys
- `muda@0.19.3`（rust，Apache-2.0 OR MIT）— https://github.com/tauri-apps/muda
- `ndk@0.9.0`（rust，MIT OR Apache-2.0）— https://github.com/rust-mobile/ndk
- `ndk-sys@0.6.0+11769913`（rust，MIT OR Apache-2.0）— https://github.com/rust-mobile/ndk
- `num-conv@0.2.2`（rust，MIT OR Apache-2.0）— https://github.com/jhpratt/num-conv
- `num_enum@0.7.6`（rust，BSD-3-Clause OR MIT OR Apache-2.0）— https://github.com/illicitonion/num_enum
- `num_enum_derive@0.7.6`（rust，BSD-3-Clause OR MIT OR Apache-2.0）— https://github.com/illicitonion/num_enum
- `objc2@0.6.4`（rust，MIT）— https://github.com/madsmtm/objc2
- `objc2-app-kit@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-cloud-kit@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-core-data@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-core-foundation@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-core-graphics@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-core-image@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-core-location@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-core-text@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-encode@4.1.0`（rust，MIT）— https://github.com/madsmtm/objc2
- `objc2-exception-helper@0.1.1`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-foundation@0.3.2`（rust，MIT）— https://github.com/madsmtm/objc2
- `objc2-io-surface@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-osa-kit@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-quartz-core@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-ui-kit@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-user-notifications@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `objc2-web-kit@0.3.2`（rust，Zlib OR Apache-2.0 OR MIT）— https://github.com/madsmtm/objc2
- `once_cell@1.21.4`（rust，MIT OR Apache-2.0）— https://github.com/matklad/once_cell
- `open@5.4.0`（rust，MIT）— https://github.com/Byron/open-rs
- `option-ext@0.2.0`（rust，MPL-2.0）— https://github.com/soc/option-ext.git
- `os_pipe@1.2.3`（rust，MIT）— https://github.com/oconnor663/os_pipe.rs
- `osakit@0.3.1`（rust，MIT OR Apache-2.0）— https://github.com/mdevils/rust-osakit
- `pango@0.18.3`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `pango-sys@0.18.0`（rust，MIT）— https://github.com/gtk-rs/gtk-rs-core
- `pin-project-lite@0.2.17`（rust，Apache-2.0 OR MIT）— https://github.com/taiki-e/pin-project-lite
- `proc-macro-crate@1.3.1`（rust，MIT OR Apache-2.0）— https://github.com/bkchr/proc-macro-crate
- `proc-macro-crate@2.0.2`（rust，MIT OR Apache-2.0）— https://github.com/bkchr/proc-macro-crate
- `proc-macro-crate@3.5.0`（rust，MIT OR Apache-2.0）— https://github.com/bkchr/proc-macro-crate
- `proc-macro2@1.0.107`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/proc-macro2
- `quote@1.0.47`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/quote
- `r-efi@5.3.0`（rust，MIT OR Apache-2.0 OR LGPL-2.1-or-later）— https://github.com/r-efi/r-efi
- `r-efi@6.0.0`（rust，MIT OR Apache-2.0 OR LGPL-2.1-or-later）— https://github.com/r-efi/r-efi
- `redox_syscall@0.5.18`（rust，MIT）— https://gitlab.redox-os.org/redox-os/syscall
- `redox_users@0.5.2`（rust，MIT）— https://gitlab.redox-os.org/redox-os/users
- `ref-cast@1.0.26`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/ref-cast
- `ref-cast-impl@1.0.26`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/ref-cast
- `rustc-hash@2.1.3`（rust，Apache-2.0 OR MIT）— https://github.com/rust-lang/rustc-hash
- `rustix@1.1.5`（rust，Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT）— https://github.com/bytecodealliance/rustix
- `rustls-platform-verifier-android@0.1.1`（rust，MIT OR Apache-2.0）— https://github.com/rustls/rustls-platform-verifier
- `rustversion@1.0.23`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/rustversion
- `security-framework@3.7.0`（rust，MIT OR Apache-2.0）— https://github.com/kornelski/rust-security-framework
- `security-framework-sys@2.17.0`（rust，MIT OR Apache-2.0）— https://github.com/kornelski/rust-security-framework
- `selectors@0.36.1`（rust，MPL-2.0）— https://github.com/servo/stylo
- `semver@1.0.28`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/semver
- `serde@1.0.229`（rust，MIT OR Apache-2.0）— https://github.com/serde-rs/serde
- `serde-untagged@0.1.9`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/serde-untagged
- `serde_core@1.0.229`（rust，MIT OR Apache-2.0）— https://github.com/serde-rs/serde
- `serde_derive@1.0.229`（rust，MIT OR Apache-2.0）— https://github.com/serde-rs/serde
- `serde_derive_internals@0.29.1`（rust，MIT OR Apache-2.0）— https://github.com/serde-rs/serde
- `serde_json@1.0.151`（rust，MIT OR Apache-2.0）— https://github.com/serde-rs/json
- `serde_repr@0.1.21`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/serde-repr
- `serde_spanned@0.6.9`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `serde_spanned@1.1.1`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `serde_with@3.21.0`（rust，MIT OR Apache-2.0）— https://github.com/jonasbb/serde_with/
- `serde_with_macros@3.21.0`（rust，MIT OR Apache-2.0）— https://github.com/jonasbb/serde_with/
- `servo_arc@0.4.3`（rust，MIT OR Apache-2.0）— https://github.com/servo/stylo
- `shared_child@1.1.1`（rust，MIT）— https://github.com/oconnor663/shared_child.rs
- `sigchld@0.2.4`（rust，MIT）— https://github.com/oconnor663/sigchld.rs
- `simd-adler32@0.3.10`（rust，MIT）— https://github.com/mcountryman/simd-adler32
- `simd_cesu8@1.2.0`（rust，Apache-2.0 OR MIT）— https://github.com/seancroach/simd_cesu8
- `simdutf8@0.1.5`（rust，MIT OR Apache-2.0）— https://github.com/rusticstuff/simdutf8
- `swift-rs@1.0.7`（rust，MIT OR Apache-2.0）— https://github.com/Brendonovich/swift-rs
- `syn@1.0.109`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/syn
- `syn@2.0.119`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/syn
- `syn@3.0.3`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/syn
- `sync_wrapper@1.0.2`（rust，Apache-2.0）— https://github.com/Actyx/sync_wrapper
- `system-configuration@0.7.0`（rust，MIT OR Apache-2.0）— https://github.com/mullvad/system-configuration-rs
- `system-configuration-sys@0.6.0`（rust，MIT OR Apache-2.0）— https://github.com/mullvad/system-configuration-rs
- `system-deps@6.2.2`（rust，MIT OR Apache-2.0）— https://github.com/gdesmott/system-deps
- `tao@0.35.3`（rust，Apache-2.0）— https://github.com/tauri-apps/tao
- `tao-macros@0.1.4`（rust，MIT OR Apache-2.0）— https://github.com/tauri-apps/tao
- `tar@0.4.46`（rust，MIT OR Apache-2.0）— https://github.com/composefs/tar-rs
- `target-lexicon@0.12.16`（rust，Apache-2.0 WITH LLVM-exception）— https://github.com/bytecodealliance/target-lexicon
- `tauri-plugin@2.6.3`（rust，Apache-2.0 OR MIT）— https://github.com/tauri-apps/tauri
- `thiserror@1.0.69`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/thiserror
- `thiserror@2.0.19`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/thiserror
- `thiserror-impl@1.0.69`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/thiserror
- `thiserror-impl@2.0.19`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/thiserror
- `time@0.3.55`（rust，MIT OR Apache-2.0）— https://github.com/time-rs/time
- `time-core@0.1.9`（rust，MIT OR Apache-2.0）— https://github.com/time-rs/time
- `time-macros@0.2.32`（rust，MIT OR Apache-2.0）— https://github.com/time-rs/time
- `tokio@1.53.1`（rust，MIT）— https://github.com/tokio-rs/tokio
- `tokio-util@0.7.19`（rust，MIT）— https://github.com/tokio-rs/tokio
- `toml@0.8.2`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml@0.9.12+spec-1.1.0`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml@1.1.4+spec-1.1.0`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml_datetime@0.7.5+spec-1.1.0`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml_datetime@1.1.1+spec-1.1.0`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml_edit@0.19.15`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml_edit@0.20.2`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml_edit@0.25.13+spec-1.1.0`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml_parser@1.1.3+spec-1.1.0`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `toml_writer@1.1.2+spec-1.1.0`（rust，MIT OR Apache-2.0）— https://github.com/toml-rs/toml
- `tray-icon@0.24.2`（rust，MIT OR Apache-2.0）— https://github.com/tauri-apps/tray-icon
- `typeid@1.0.3`（rust，MIT OR Apache-2.0）— https://github.com/dtolnay/typeid
- `unic-char-property@0.9.0`（rust，MIT/Apache-2.0）— https://github.com/open-i18n/rust-unic/
- `unic-char-range@0.9.0`（rust，MIT/Apache-2.0）— https://github.com/open-i18n/rust-unic/
- `unic-common@0.9.0`（rust，MIT/Apache-2.0）— https://github.com/open-i18n/rust-unic/
- `unic-ucd-ident@0.9.0`（rust，MIT/Apache-2.0）— https://github.com/open-i18n/rust-unic/
- `unic-ucd-version@0.9.0`（rust，MIT/Apache-2.0）— https://github.com/open-i18n/rust-unic/
- `untrusted@0.9.0`（rust，ISC）— https://github.com/briansmith/untrusted
- `wasi@0.11.1+wasi-snapshot-preview1`（rust，Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT）— https://github.com/bytecodealliance/wasi
- `wasip2@1.0.4+wasi-0.2.12`（rust，Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT）— https://github.com/bytecodealliance/wasi-rs
- `wasm-bindgen@0.2.126`（rust，MIT OR Apache-2.0）— https://github.com/wasm-bindgen/wasm-bindgen
- `wasm-bindgen-futures@0.4.76`（rust，MIT OR Apache-2.0）— https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/futures
- `wasm-bindgen-macro@0.2.126`（rust，MIT OR Apache-2.0）— https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro
- `wasm-bindgen-macro-support@0.2.126`（rust，MIT OR Apache-2.0）— https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro-support
- `wasm-bindgen-shared@0.2.126`（rust，MIT OR Apache-2.0）— https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/shared
- `wasm-streams@0.5.0`（rust，MIT OR Apache-2.0）— https://github.com/MattiasBuelens/wasm-streams/
- `web-sys@0.3.103`（rust，MIT OR Apache-2.0）— https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/web-sys
- `webpki-root-certs@1.0.9`（rust，CDLA-Permissive-2.0）— https://github.com/rustls/webpki-roots
- `webview2-com@0.38.2`（rust，MIT）— https://github.com/wravery/webview2-rs
- `webview2-com-macros@0.8.1`（rust，MIT）— https://github.com/wravery/webview2-rs
- `webview2-com-sys@0.38.2`（rust，MIT）— https://github.com/wravery/webview2-rs
- `winapi-i686-pc-windows-gnu@0.4.0`（rust，MIT OR Apache-2.0）— https://github.com/retep998/winapi-rs
- `winapi-x86_64-pc-windows-gnu@0.4.0`（rust，MIT OR Apache-2.0）— https://github.com/retep998/winapi-rs
- `window-vibrancy@0.6.0`（rust，Apache-2.0 OR MIT）— https://github.com/tauri-apps/tauri-plugin-vibrancy
- `windows@0.61.3`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-collections@0.2.0`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-core@0.61.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-core@0.62.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-future@0.2.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-implement@0.60.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-interface@0.59.3`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-link@0.1.3`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-link@0.2.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-numerics@0.2.0`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-registry@0.6.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-result@0.3.4`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-result@0.4.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-strings@0.4.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-strings@0.5.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-sys@0.45.0`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-sys@0.52.0`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-sys@0.59.0`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-sys@0.60.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-sys@0.61.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-targets@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-targets@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-targets@0.53.5`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-threading@0.1.0`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows-version@0.1.7`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_aarch64_gnullvm@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_aarch64_gnullvm@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_aarch64_gnullvm@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_aarch64_msvc@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_aarch64_msvc@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_aarch64_msvc@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_gnu@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_gnu@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_gnu@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_gnullvm@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_gnullvm@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_msvc@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_msvc@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_i686_msvc@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_gnu@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_gnu@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_gnu@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_gnullvm@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_gnullvm@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_gnullvm@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_msvc@0.42.2`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_msvc@0.52.6`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `windows_x86_64_msvc@0.53.1`（rust，MIT OR Apache-2.0）— https://github.com/microsoft/windows-rs
- `winnow@0.5.40`（rust，MIT）— https://github.com/winnow-rs/winnow
- `winnow@0.7.15`（rust，MIT）— https://github.com/winnow-rs/winnow
- `winnow@1.0.4`（rust，MIT）— https://github.com/winnow-rs/winnow
- `wit-bindgen@0.57.1`（rust，Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT）— https://github.com/bytecodealliance/wit-bindgen
- `wry@0.55.1`（rust，Apache-2.0 OR MIT）— https://github.com/tauri-apps/wry
- `x11@2.21.0`（rust，MIT）— https://github.com/AltF02/x11-rs.git
- `x11-dl@2.21.0`（rust，MIT）— https://github.com/AltF02/x11-rs.git
- `zeroize@1.9.0`（rust，Apache-2.0 OR MIT）— https://github.com/RustCrypto/utils
- `zmij@1.0.23`（rust，MIT）— https://github.com/dtolnay/zmij

## 许可证全文

双许可表达式（例如 `MIT OR Apache-2.0`、`Apache-2.0 WITH LLVM-exception`）按其中的每个标识各附一份全文。

### 0BSD

```
Copyright (C) YEAR by AUTHOR EMAIL

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

### Apache-2.0

```
Apache License
Version 2.0, January 2004
http://www.apache.org/licenses/

TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

1. Definitions.

"License" shall mean the terms and conditions for use, reproduction, and distribution as defined by Sections 1 through 9 of this document.

"Licensor" shall mean the copyright owner or entity authorized by the copyright owner that is granting the License.

"Legal Entity" shall mean the union of the acting entity and all other entities that control, are controlled by, or are under common control with that entity. For the purposes of this definition, "control" means (i) the power, direct or indirect, to cause the direction or management of such entity, whether by contract or otherwise, or (ii) ownership of fifty percent (50%) or more of the outstanding shares, or (iii) beneficial ownership of such entity.

"You" (or "Your") shall mean an individual or Legal Entity exercising permissions granted by this License.

"Source" form shall mean the preferred form for making modifications, including but not limited to software source code, documentation source, and configuration files.

"Object" form shall mean any form resulting from mechanical transformation or translation of a Source form, including but not limited to compiled object code, generated documentation, and conversions to other media types.

"Work" shall mean the work of authorship, whether in Source or Object form, made available under the License, as indicated by a copyright notice that is included in or attached to the work (an example is provided in the Appendix below).

"Derivative Works" shall mean any work, whether in Source or Object form, that is based on (or derived from) the Work and for which the editorial revisions, annotations, elaborations, or other modifications represent, as a whole, an original work of authorship. For the purposes of this License, Derivative Works shall not include works that remain separable from, or merely link (or bind by name) to the interfaces of, the Work and Derivative Works thereof.

"Contribution" shall mean any work of authorship, including the original version of the Work and any modifications or additions to that Work or Derivative Works thereof, that is intentionally submitted to Licensor for inclusion in the Work by the copyright owner or by an individual or Legal Entity authorized to submit on behalf of the copyright owner. For the purposes of this definition, "submitted" means any form of electronic, verbal, or written communication sent to the Licensor or its representatives, including but not limited to communication on electronic mailing lists, source code control systems, and issue tracking systems that are managed by, or on behalf of, the Licensor for the purpose of discussing and improving the Work, but excluding communication that is conspicuously marked or otherwise designated in writing by the copyright owner as "Not a Contribution."

"Contributor" shall mean Licensor and any individual or Legal Entity on behalf of whom a Contribution has been received by Licensor and subsequently incorporated within the Work.

2. Grant of Copyright License. Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to reproduce, prepare Derivative Works of, publicly display, publicly perform, sublicense, and distribute the Work and such Derivative Works in Source or Object form.

3. Grant of Patent License. Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable (except as stated in this section) patent license to make, have made, use, offer to sell, sell, import, and otherwise transfer the Work, where such license applies only to those patent claims licensable by such Contributor that are necessarily infringed by their Contribution(s) alone or by combination of their Contribution(s) with the Work to which such Contribution(s) was submitted. If You institute patent litigation against any entity (including a cross-claim or counterclaim in a lawsuit) alleging that the Work or a Contribution incorporated within the Work constitutes direct or contributory patent infringement, then any patent licenses granted to You under this License for that Work shall terminate as of the date such litigation is filed.

4. Redistribution. You may reproduce and distribute copies of the Work or Derivative Works thereof in any medium, with or without modifications, and in Source or Object form, provided that You meet the following conditions:

     (a) You must give any other recipients of the Work or Derivative Works a copy of this License; and

     (b) You must cause any modified files to carry prominent notices stating that You changed the files; and

     (c) You must retain, in the Source form of any Derivative Works that You distribute, all copyright, patent, trademark, and attribution notices from the Source form of the Work, excluding those notices that do not pertain to any part of the Derivative Works; and

     (d) If the Work includes a "NOTICE" text file as part of its distribution, then any Derivative Works that You distribute must include a readable copy of the attribution notices contained within such NOTICE file, excluding those notices that do not pertain to any part of the Derivative Works, in at least one of the following places: within a NOTICE text file distributed as part of the Derivative Works; within the Source form or documentation, if provided along with the Derivative Works; or, within a display generated by the Derivative Works, if and wherever such third-party notices normally appear. The contents of the NOTICE file are for informational purposes only and do not modify the License. You may add Your own attribution notices within Derivative Works that You distribute, alongside or as an addendum to the NOTICE text from the Work, provided that such additional attribution notices cannot be construed as modifying the License.

     You may add Your own copyright statement to Your modifications and may provide additional or different license terms and conditions for use, reproduction, or distribution of Your modifications, or for any such Derivative Works as a whole, provided Your use, reproduction, and distribution of the Work otherwise complies with the conditions stated in this License.

5. Submission of Contributions. Unless You explicitly state otherwise, any Contribution intentionally submitted for inclusion in the Work by You to the Licensor shall be under the terms and conditions of this License, without any additional terms or conditions. Notwithstanding the above, nothing herein shall supersede or modify the terms of any separate license agreement you may have executed with Licensor regarding such Contributions.

6. Trademarks. This License does not grant permission to use the trade names, trademarks, service marks, or product names of the Licensor, except as required for reasonable and customary use in describing the origin of the Work and reproducing the content of the NOTICE file.

7. Disclaimer of Warranty. Unless required by applicable law or agreed to in writing, Licensor provides the Work (and each Contributor provides its Contributions) on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied, including, without limitation, any warranties or conditions of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE. You are solely responsible for determining the appropriateness of using or redistributing the Work and assume any risks associated with Your exercise of permissions under this License.

8. Limitation of Liability. In no event and under no legal theory, whether in tort (including negligence), contract, or otherwise, unless required by applicable law (such as deliberate and grossly negligent acts) or agreed to in writing, shall any Contributor be liable to You for damages, including any direct, indirect, special, incidental, or consequential damages of any character arising as a result of this License or out of the use or inability to use the Work (including but not limited to damages for loss of goodwill, work stoppage, computer failure or malfunction, or any and all other commercial damages or losses), even if such Contributor has been advised of the possibility of such damages.

9. Accepting Warranty or Additional Liability. While redistributing the Work or Derivative Works thereof, You may choose to offer, and charge a fee for, acceptance of support, warranty, indemnity, or other liability obligations and/or rights consistent with this License. However, in accepting such obligations, You may act only on Your own behalf and on Your sole responsibility, not on behalf of any other Contributor, and only if You agree to indemnify, defend, and hold each Contributor harmless for any liability incurred by, or claims asserted against, such Contributor by reason of your accepting any such warranty or additional liability.

END OF TERMS AND CONDITIONS

APPENDIX: How to apply the Apache License to your work.

To apply the Apache License to your work, attach the following boilerplate notice, with the fields enclosed by brackets "[]" replaced with your own identifying information. (Don't include the brackets!)  The text should be enclosed in the appropriate comment syntax for the file format. We also recommend that a file or class name and description of purpose be included on the same "printed page" as the copyright notice for easier identification within third-party archives.

Copyright [yyyy] [name of copyright owner]

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

### BSD-2-Clause

```
Copyright (c) <year> <owner> 

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### BSD-3-Clause

```
Copyright (c) <year> <owner>. 

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### BlueOak-1.0.0

```
# Blue Oak Model License

Version 1.0.0

## Purpose

This license gives everyone as much permission to work with
this software as possible, while protecting contributors
from liability.

## Acceptance

In order to receive this license, you must agree to its
rules.  The rules of this license are both obligations
under that agreement and conditions to your license.
You must not do anything with this software that triggers
a rule that you cannot or will not follow.

## Copyright

Each contributor licenses you to do everything with this
software that would otherwise infringe that contributor's
copyright in it.

## Notices

You must ensure that everyone who gets a copy of
any part of this software from you, with or without
changes, also gets the text of this license or a link to
<https://blueoakcouncil.org/license/1.0.0>.

## Excuse

If anyone notifies you in writing that you have not
complied with [Notices](#notices), you can keep your
license by taking all practical steps to comply within 30
days after the notice.  If you do not do so, your license
ends immediately.

## Patent

Each contributor licenses you to do everything with this
software that would otherwise infringe any patent claims
they can license or become able to license.

## Reliability

No contributor can revoke this license.

## No Liability

***As far as the law allows, this software comes as is,
without any warranty or condition, and no contributor
will be liable to anyone for any damages related to this
software or this license, under any kind of legal claim.***
```

### CC0-1.0

```
Creative Commons Legal Code

CC0 1.0 Universal

    CREATIVE COMMONS CORPORATION IS NOT A LAW FIRM AND DOES NOT PROVIDE
    LEGAL SERVICES. DISTRIBUTION OF THIS DOCUMENT DOES NOT CREATE AN
    ATTORNEY-CLIENT RELATIONSHIP. CREATIVE COMMONS PROVIDES THIS
    INFORMATION ON AN "AS-IS" BASIS. CREATIVE COMMONS MAKES NO WARRANTIES
    REGARDING THE USE OF THIS DOCUMENT OR THE INFORMATION OR WORKS
    PROVIDED HEREUNDER, AND DISCLAIMS LIABILITY FOR DAMAGES RESULTING FROM
    THE USE OF THIS DOCUMENT OR THE INFORMATION OR WORKS PROVIDED
    HEREUNDER.

Statement of Purpose

The laws of most jurisdictions throughout the world automatically confer
exclusive Copyright and Related Rights (defined below) upon the creator
and subsequent owner(s) (each and all, an "owner") of an original work of
authorship and/or a database (each, a "Work").

Certain owners wish to permanently relinquish those rights to a Work for
the purpose of contributing to a commons of creative, cultural and
scientific works ("Commons") that the public can reliably and without fear
of later claims of infringement build upon, modify, incorporate in other
works, reuse and redistribute as freely as possible in any form whatsoever
and for any purposes, including without limitation commercial purposes.
These owners may contribute to the Commons to promote the ideal of a free
culture and the further production of creative, cultural and scientific
works, or to gain reputation or greater distribution for their Work in
part through the use and efforts of others.

For these and/or other purposes and motivations, and without any
expectation of additional consideration or compensation, the person
associating CC0 with a Work (the "Affirmer"), to the extent that he or she
is an owner of Copyright and Related Rights in the Work, voluntarily
elects to apply CC0 to the Work and publicly distribute the Work under its
terms, with knowledge of his or her Copyright and Related Rights in the
Work and the meaning and intended legal effect of CC0 on those rights.

1. Copyright and Related Rights. A Work made available under CC0 may be
protected by copyright and related or neighboring rights ("Copyright and
Related Rights"). Copyright and Related Rights include, but are not
limited to, the following:

  i. the right to reproduce, adapt, distribute, perform, display,
     communicate, and translate a Work;
 ii. moral rights retained by the original author(s) and/or performer(s);
iii. publicity and privacy rights pertaining to a person's image or
     likeness depicted in a Work;
 iv. rights protecting against unfair competition in regards to a Work,
     subject to the limitations in paragraph 4(a), below;
  v. rights protecting the extraction, dissemination, use and reuse of data
     in a Work;
 vi. database rights (such as those arising under Directive 96/9/EC of the
     European Parliament and of the Council of 11 March 1996 on the legal
     protection of databases, and under any national implementation
     thereof, including any amended or successor version of such
     directive); and
vii. other similar, equivalent or corresponding rights throughout the
     world based on applicable law or treaty, and any national
     implementations thereof.

2. Waiver. To the greatest extent permitted by, but not in contravention
of, applicable law, Affirmer hereby overtly, fully, permanently,
irrevocably and unconditionally waives, abandons, and surrenders all of
Affirmer's Copyright and Related Rights and associated claims and causes
of action, whether now known or unknown (including existing as well as
future claims and causes of action), in the Work (i) in all territories
worldwide, (ii) for the maximum duration provided by applicable law or
treaty (including future time extensions), (iii) in any current or future
medium and for any number of copies, and (iv) for any purpose whatsoever,
including without limitation commercial, advertising or promotional
purposes (the "Waiver"). Affirmer makes the Waiver for the benefit of each
member of the public at large and to the detriment of Affirmer's heirs and
successors, fully intending that such Waiver shall not be subject to
revocation, rescission, cancellation, termination, or any other legal or
equitable action to disrupt the quiet enjoyment of the Work by the public
as contemplated by Affirmer's express Statement of Purpose.

3. Public License Fallback. Should any part of the Waiver for any reason
be judged legally invalid or ineffective under applicable law, then the
Waiver shall be preserved to the maximum extent permitted taking into
account Affirmer's express Statement of Purpose. In addition, to the
extent the Waiver is so judged Affirmer hereby grants to each affected
person a royalty-free, non transferable, non sublicensable, non exclusive,
irrevocable and unconditional license to exercise Affirmer's Copyright and
Related Rights in the Work (i) in all territories worldwide, (ii) for the
maximum duration provided by applicable law or treaty (including future
time extensions), (iii) in any current or future medium and for any number
of copies, and (iv) for any purpose whatsoever, including without
limitation commercial, advertising or promotional purposes (the
"License"). The License shall be deemed effective as of the date CC0 was
applied by Affirmer to the Work. Should any part of the License for any
reason be judged legally invalid or ineffective under applicable law, such
partial invalidity or ineffectiveness shall not invalidate the remainder
of the License, and in such case Affirmer hereby affirms that he or she
will not (i) exercise any of his or her remaining Copyright and Related
Rights in the Work or (ii) assert any associated claims and causes of
action with respect to the Work, in either case contrary to Affirmer's
express Statement of Purpose.

4. Limitations and Disclaimers.

 a. No trademark or patent rights held by Affirmer are waived, abandoned,
    surrendered, licensed or otherwise affected by this document.
 b. Affirmer offers the Work as-is and makes no representations or
    warranties of any kind concerning the Work, express, implied,
    statutory or otherwise, including without limitation warranties of
    title, merchantability, fitness for a particular purpose, non
    infringement, or the absence of latent or other defects, accuracy, or
    the present or absence of errors, whether or not discoverable, all to
    the greatest extent permissible under applicable law.
 c. Affirmer disclaims responsibility for clearing rights of other persons
    that may apply to the Work or any use thereof, including without
    limitation any person's Copyright and Related Rights in the Work.
    Further, Affirmer disclaims responsibility for obtaining any necessary
    consents, permissions or other rights required for any use of the
    Work.
 d. Affirmer understands and acknowledges that Creative Commons is not a
    party to this document and has no duty or obligation with respect to
    this CC0 or use of the Work.
```

### CDLA-Permissive-2.0

```
Community Data License Agreement - Permissive - Version 2.0

This is the Community Data License Agreement - Permissive, Version 2.0 (the "agreement"). Data Provider(s) and Data Recipient(s) agree as follows:

1. Provision of the Data

1.1. A Data Recipient may use, modify, and share the Data made available by Data Provider(s) under this agreement if that Data Recipient follows the terms of this agreement.

1.2. This agreement does not impose any restriction on a Data Recipient's use, modification, or sharing of any portions of the Data that are in the public domain or that may be used, modified, or shared under any other legal exception or limitation.

2. Conditions for Sharing Data

2.1. A Data Recipient may share Data, with or without modifications, so long as the Data Recipient makes available the text of this agreement with the shared Data.

3. No Restrictions on Results

3.1. This agreement does not impose any restriction or obligations with respect to the use, modification, or sharing of Results.

4. No Warranty; Limitation of Liability

4.1. All Data Recipients receive the Data subject to the following terms:

THE DATA IS PROVIDED ON AN "AS IS" BASIS, WITHOUT REPRESENTATIONS, WARRANTIES OR CONDITIONS OF ANY KIND, EITHER EXPRESS OR IMPLIED INCLUDING, WITHOUT LIMITATION, ANY WARRANTIES OR CONDITIONS OF TITLE, NON-INFRINGEMENT, MERCHANTABILITY OR FITNESS FOR A PARTICULAR PURPOSE.

NO DATA PROVIDER SHALL HAVE ANY LIABILITY FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING WITHOUT LIMITATION LOST PROFITS), HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE DATA OR RESULTS, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.

5. Definitions

5.1. "Data" means the material received by a Data Recipient under this agreement.

5.2. "Data Provider" means any person who is the source of Data provided under this agreement and in reliance on a Data Recipient's agreement to its terms.

5.3. "Data Recipient" means any person who receives Data directly or indirectly from a Data Provider and agrees to the terms of this agreement.

5.4. "Results" means any outcome obtained by computational analysis of Data, including for example machine learning models and models' insights.
```

### CNRI-Python

```
CNRI OPEN SOURCE LICENSE AGREEMENT

IMPORTANT: PLEASE READ THE FOLLOWING AGREEMENT CAREFULLY.

BY CLICKING ON "ACCEPT" WHERE INDICATED BELOW, OR BY COPYING, INSTALLING OR OTHERWISE USING PYTHON 1.6, beta 1 SOFTWARE, YOU ARE DEEMED TO HAVE AGREED TO THE TERMS AND CONDITIONS OF THIS LICENSE AGREEMENT.

1. This LICENSE AGREEMENT is between the Corporation for National Research Initiatives, having an office at 1895 Preston White Drive, Reston, VA 20191 ("CNRI"), and the Individual or Organization ("Licensee") accessing and otherwise using Python 1.6, beta 1 software in source or binary form and its associated documentation, as released at the www.python.org Internet site on August 4, 2000 ("Python 1.6b1").

2. Subject to the terms and conditions of this License Agreement, CNRI hereby grants Licensee a non-exclusive, royalty-free, world-wide license to reproduce, analyze, test, perform and/or display publicly, prepare derivative works, distribute, and otherwise use Python 1.6b1 alone or in any derivative version, provided, however, that CNRIs License Agreement is retained in Python 1.6b1, alone or in any derivative version prepared by Licensee.

Alternately, in lieu of CNRIs License Agreement, Licensee may substitute the following text (omitting the quotes): "Python 1.6, beta 1, is made available subject to the terms and conditions in CNRIs License Agreement. This Agreement may be located on the Internet using the following unique, persistent identifier (known as a handle): 1895.22/1011. This Agreement may also be obtained from a proxy server on the Internet using the URL:http://hdl.handle.net/1895.22/1011".

3. In the event Licensee prepares a derivative work that is based on or incorporates Python 1.6b1 or any part thereof, and wants to make the derivative work available to the public as provided herein, then Licensee hereby agrees to indicate in any such work the nature of the modifications made to Python 1.6b1.

4. CNRI is making Python 1.6b1 available to Licensee on an "AS IS" basis. CNRI MAKES NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR IMPLIED. BY WAY OF EXAMPLE, BUT NOT LIMITATION, CNRI MAKES NO AND DISCLAIMS ANY REPRESENTATION OR WARRANTY OF MERCHANTABILITY OR FITNESS FOR ANY PARTICULAR PURPOSE OR THAT THE USE OF PYTHON 1.6b1 WILL NOT INFRINGE ANY THIRD PARTY RIGHTS.

5. CNRI SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF THE SOFTWARE FOR ANY INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS AS A RESULT OF USING, MODIFYING OR DISTRIBUTING PYTHON 1.6b1, OR ANY DERIVATIVE THEREOF, EVEN IF ADVISED OF THE POSSIBILITY THEREOF.

6. This License Agreement will automatically terminate upon a material breach of its terms and conditions.

7. This License Agreement shall be governed by and interpreted in all respects by the law of the State of Virginia, excluding conflict of law provisions. Nothing in this License Agreement shall be deemed to create any relationship of agency, partnership, or joint venture between CNRI and Licensee. This License Agreement does not grant permission to use CNRI trademarks or trade name in a trademark sense to endorse or promote products or services of Licensee, or any third party.

8. By clicking on the "ACCEPT" button where indicated, or by copying, installing or otherwise using Python 1.6b1, Licensee agrees to be bound by the terms and conditions of this License Agreement.

ACCEPT
```

### ISC

```
ISC License

Copyright <year> <owner> 

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

### LGPL-2.1-or-later

```
GNU LESSER GENERAL PUBLIC LICENSE

Version 2.1, February 1999

Copyright (C) 1991, 1999 Free Software Foundation, Inc.
51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

Everyone is permitted to copy and distribute verbatim copies of this license document, but changing it is not allowed.

[This is the first released version of the Lesser GPL.  It also counts as the successor of the GNU Library Public License, version 2, hence the version number 2.1.]

Preamble

The licenses for most software are designed to take away your freedom to share and change it. By contrast, the GNU General Public Licenses are intended to guarantee your freedom to share and change free software--to make sure the software is free for all its users.

This license, the Lesser General Public License, applies to some specially designated software packages--typically libraries--of the Free Software Foundation and other authors who decide to use it. You can use it too, but we suggest you first think carefully about whether this license or the ordinary General Public License is the better strategy to use in any particular case, based on the explanations below.

When we speak of free software, we are referring to freedom of use, not price. Our General Public Licenses are designed to make sure that you have the freedom to distribute copies of free software (and charge for this service if you wish); that you receive source code or can get it if you want it; that you can change the software and use pieces of it in new free programs; and that you are informed that you can do these things.

To protect your rights, we need to make restrictions that forbid distributors to deny you these rights or to ask you to surrender these rights. These restrictions translate to certain responsibilities for you if you distribute copies of the library or if you modify it.

For example, if you distribute copies of the library, whether gratis or for a fee, you must give the recipients all the rights that we gave you. You must make sure that they, too, receive or can get the source code. If you link other code with the library, you must provide complete object files to the recipients, so that they can relink them with the library after making changes to the library and recompiling it. And you must show them these terms so they know their rights.

We protect your rights with a two-step method: (1) we copyright the library, and (2) we offer you this license, which gives you legal permission to copy, distribute and/or modify the library.

To protect each distributor, we want to make it very clear that there is no warranty for the free library. Also, if the library is modified by someone else and passed on, the recipients should know that what they have is not the original version, so that the original author's reputation will not be affected by problems that might be introduced by others.

Finally, software patents pose a constant threat to the existence of any free program. We wish to make sure that a company cannot effectively restrict the users of a free program by obtaining a restrictive license from a patent holder. Therefore, we insist that any patent license obtained for a version of the library must be consistent with the full freedom of use specified in this license.

Most GNU software, including some libraries, is covered by the ordinary GNU General Public License. This license, the GNU Lesser General Public License, applies to certain designated libraries, and is quite different from the ordinary General Public License. We use this license for certain libraries in order to permit linking those libraries into non-free programs.

When a program is linked with a library, whether statically or using a shared library, the combination of the two is legally speaking a combined work, a derivative of the original library. The ordinary General Public License therefore permits such linking only if the entire combination fits its criteria of freedom. The Lesser General Public License permits more lax criteria for linking other code with the library.

We call this license the "Lesser" General Public License because it does Less to protect the user's freedom than the ordinary General Public License. It also provides other free software developers Less of an advantage over competing non-free programs. These disadvantages are the reason we use the ordinary General Public License for many libraries. However, the Lesser license provides advantages in certain special circumstances.

For example, on rare occasions, there may be a special need to encourage the widest possible use of a certain library, so that it becomes a de-facto standard. To achieve this, non-free programs must be allowed to use the library. A more frequent case is that a free library does the same job as widely used non-free libraries. In this case, there is little to gain by limiting the free library to free software only, so we use the Lesser General Public License.

In other cases, permission to use a particular library in non-free programs enables a greater number of people to use a large body of free software. For example, permission to use the GNU C Library in non-free programs enables many more people to use the whole GNU operating system, as well as its variant, the GNU/Linux operating system.

Although the Lesser General Public License is Less protective of the users' freedom, it does ensure that the user of a program that is linked with the Library has the freedom and the wherewithal to run that program using a modified version of the Library.

The precise terms and conditions for copying, distribution and modification follow. Pay close attention to the difference between a "work based on the library" and a "work that uses the library". The former contains code derived from the library, whereas the latter must be combined with the library in order to run.

GNU LESSER GENERAL PUBLIC LICENSE
TERMS AND CONDITIONS FOR COPYING, DISTRIBUTION AND MODIFICATION

0. This License Agreement applies to any software library or other program which contains a notice placed by the copyright holder or other authorized party saying it may be distributed under the terms of this Lesser General Public License (also called "this License"). Each licensee is addressed as "you".

A "library" means a collection of software functions and/or data prepared so as to be conveniently linked with application programs (which use some of those functions and data) to form executables.

The "Library", below, refers to any such software library or work which has been distributed under these terms. A "work based on the Library" means either the Library or any derivative work under copyright law: that is to say, a work containing the Library or a portion of it, either verbatim or with modifications and/or translated straightforwardly into another language. (Hereinafter, translation is included without limitation in the term "modification".)

"Source code" for a work means the preferred form of the work for making modifications to it. For a library, complete source code means all the source code for all modules it contains, plus any associated interface definition files, plus the scripts used to control compilation and installation of the library.

Activities other than copying, distribution and modification are not covered by this License; they are outside its scope. The act of running a program using the Library is not restricted, and output from such a program is covered only if its contents constitute a work based on the Library (independent of the use of the Library in a tool for writing it). Whether that is true depends on what the Library does and what the program that uses the Library does.

1. You may copy and distribute verbatim copies of the Library's complete source code as you receive it, in any medium, provided that you conspicuously and appropriately publish on each copy an appropriate copyright notice and disclaimer of warranty; keep intact all the notices that refer to this License and to the absence of any warranty; and distribute a copy of this License along with the Library.

You may charge a fee for the physical act of transferring a copy, and you may at your option offer warranty protection in exchange for a fee.

2. You may modify your copy or copies of the Library or any portion of it, thus forming a work based on the Library, and copy and distribute such modifications or work under the terms of Section 1 above, provided that you also meet all of these conditions:

     a) The modified work must itself be a software library.

     b) You must cause the files modified to carry prominent notices stating that you changed the files and the date of any change.

     c) You must cause the whole of the work to be licensed at no charge to all third parties under the terms of this License.

     d) If a facility in the modified Library refers to a function or a table of data to be supplied by an application program that uses the facility, other than as an argument passed when the facility is invoked, then you must make a good faith effort to ensure that, in the event an application does not supply such function or table, the facility still operates, and performs whatever part of its purpose remains meaningful.

(For example, a function in a library to compute square roots has a purpose that is entirely well-defined independent of the application. Therefore, Subsection 2d requires that any application-supplied function or table used by this function must be optional: if the application does not supply it, the square root function must still compute square roots.)

These requirements apply to the modified work as a whole. If identifiable sections of that work are not derived from the Library, and can be reasonably considered independent and separate works in themselves, then this License, and its terms, do not apply to those sections when you distribute them as separate works. But when you distribute the same sections as part of a whole which is a work based on the Library, the distribution of the whole must be on the terms of this License, whose permissions for other licensees extend to the entire whole, and thus to each and every part regardless of who wrote it.

Thus, it is not the intent of this section to claim rights or contest your rights to work written entirely by you; rather, the intent is to exercise the right to control the distribution of derivative or collective works based on the Library.

In addition, mere aggregation of another work not based on the Library with the Library (or with a work based on the Library) on a volume of a storage or distribution medium does not bring the other work under the scope of this License.

3. You may opt to apply the terms of the ordinary GNU General Public License instead of this License to a given copy of the Library. To do this, you must alter all the notices that refer to this License, so that they refer to the ordinary GNU General Public License, version 2, instead of to this License. (If a newer version than version 2 of the ordinary GNU General Public License has appeared, then you can specify that version instead if you wish.) Do not make any other change in these notices.

Once this change is made in a given copy, it is irreversible for that copy, so the ordinary GNU General Public License applies to all subsequent copies and derivative works made from that copy.

This option is useful when you wish to copy part of the code of the Library into a program that is not a library.

4. You may copy and distribute the Library (or a portion or derivative of it, under Section 2) in object code or executable form under the terms of Sections 1 and 2 above provided that you accompany it with the complete corresponding machine-readable source code, which must be distributed under the terms of Sections 1 and 2 above on a medium customarily used for software interchange.

If distribution of object code is made by offering access to copy from a designated place, then offering equivalent access to copy the source code from the same place satisfies the requirement to distribute the source code, even though third parties are not compelled to copy the source along with the object code.

5. A program that contains no derivative of any portion of the Library, but is designed to work with the Library by being compiled or linked with it, is called a "work that uses the Library". Such a work, in isolation, is not a derivative work of the Library, and therefore falls outside the scope of this License.

However, linking a "work that uses the Library" with the Library creates an executable that is a derivative of the Library (because it contains portions of the Library), rather than a "work that uses the library". The executable is therefore covered by this License. Section 6 states terms for distribution of such executables.

When a "work that uses the Library" uses material from a header file that is part of the Library, the object code for the work may be a derivative work of the Library even though the source code is not. Whether this is true is especially significant if the work can be linked without the Library, or if the work is itself a library. The threshold for this to be true is not precisely defined by law.

If such an object file uses only numerical parameters, data structure layouts and accessors, and small macros and small inline functions (ten lines or less in length), then the use of the object file is unrestricted, regardless of whether it is legally a derivative work. (Executables containing this object code plus portions of the Library will still fall under Section 6.)

Otherwise, if the work is a derivative of the Library, you may distribute the object code for the work under the terms of Section 6. Any executables containing that work also fall under Section 6, whether or not they are linked directly with the Library itself.

6. As an exception to the Sections above, you may also combine or link a "work that uses the Library" with the Library to produce a work containing portions of the Library, and distribute that work under terms of your choice, provided that the terms permit modification of the work for the customer's own use and reverse engineering for debugging such modifications.

You must give prominent notice with each copy of the work that the Library is used in it and that the Library and its use are covered by this License. You must supply a copy of this License. If the work during execution displays copyright notices, you must include the copyright notice for the Library among them, as well as a reference directing the user to the copy of this License. Also, you must do one of these things:

     a) Accompany the work with the complete corresponding machine-readable source code for the Library including whatever changes were used in the work (which must be distributed under Sections 1 and 2 above); and, if the work is an executable linked with the Library, with the complete machine-readable "work that uses the Library", as object code and/or source code, so that the user can modify the Library and then relink to produce a modified executable containing the modified Library. (It is understood that the user who changes the contents of definitions files in the Library will not necessarily be able to recompile the application to use the modified definitions.)

     b) Use a suitable shared library mechanism for linking with the Library. A suitable mechanism is one that (1) uses at run time a copy of the library already present on the user's computer system, rather than copying library functions into the executable, and (2) will operate properly with a modified version of the library, if the user installs one, as long as the modified version is interface-compatible with the version that the work was made with.

     c) Accompany the work with a written offer, valid for at least three years, to give the same user the materials specified in Subsection 6a, above, for a charge no more than the cost of performing this distribution.

     d) If distribution of the work is made by offering access to copy from a designated place, offer equivalent access to copy the above specified materials from the same place.

     e) Verify that the user has already received a copy of these materials or that you have already sent this user a copy.

For an executable, the required form of the "work that uses the Library" must include any data and utility programs needed for reproducing the executable from it. However, as a special exception, the materials to be distributed need not include anything that is normally distributed (in either source or binary form) with the major components (compiler, kernel, and so on) of the operating system on which the executable runs, unless that component itself accompanies the executable.

It may happen that this requirement contradicts the license restrictions of other proprietary libraries that do not normally accompany the operating system. Such a contradiction means you cannot use both them and the Library together in an executable that you distribute.

7. You may place library facilities that are a work based on the Library side-by-side in a single library together with other library facilities not covered by this License, and distribute such a combined library, provided that the separate distribution of the work based on the Library and of the other library facilities is otherwise permitted, and provided that you do these two things:

     a) Accompany the combined library with a copy of the same work based on the Library, uncombined with any other library facilities. This must be distributed under the terms of the Sections above.

     b) Give prominent notice with the combined library of the fact that part of it is a work based on the Library, and explaining where to find the accompanying uncombined form of the same work.

8. You may not copy, modify, sublicense, link with, or distribute the Library except as expressly provided under this License. Any attempt otherwise to copy, modify, sublicense, link with, or distribute the Library is void, and will automatically terminate your rights under this License. However, parties who have received copies, or rights, from you under this License will not have their licenses terminated so long as such parties remain in full compliance.

9. You are not required to accept this License, since you have not signed it. However, nothing else grants you permission to modify or distribute the Library or its derivative works. These actions are prohibited by law if you do not accept this License. Therefore, by modifying or distributing the Library (or any work based on the Library), you indicate your acceptance of this License to do so, and all its terms and conditions for copying, distributing or modifying the Library or works based on it.

10. Each time you redistribute the Library (or any work based on the Library), the recipient automatically receives a license from the original licensor to copy, distribute, link with or modify the Library subject to these terms and conditions. You may not impose any further restrictions on the recipients' exercise of the rights granted herein. You are not responsible for enforcing compliance by third parties with this License.

11. If, as a consequence of a court judgment or allegation of patent infringement or for any other reason (not limited to patent issues), conditions are imposed on you (whether by court order, agreement or otherwise) that contradict the conditions of this License, they do not excuse you from the conditions of this License. If you cannot distribute so as to satisfy simultaneously your obligations under this License and any other pertinent obligations, then as a consequence you may not distribute the Library at all. For example, if a patent license would not permit royalty-free redistribution of the Library by all those who receive copies directly or indirectly through you, then the only way you could satisfy both it and this License would be to refrain entirely from distribution of the Library.

If any portion of this section is held invalid or unenforceable under any particular circumstance, the balance of the section is intended to apply, and the section as a whole is intended to apply in other circumstances.

It is not the purpose of this section to induce you to infringe any patents or other property right claims or to contest validity of any such claims; this section has the sole purpose of protecting the integrity of the free software distribution system which is implemented by public license practices. Many people have made generous contributions to the wide range of software distributed through that system in reliance on consistent application of that system; it is up to the author/donor to decide if he or she is willing to distribute software through any other system and a licensee cannot impose that choice.

This section is intended to make thoroughly clear what is believed to be a consequence of the rest of this License.

12. If the distribution and/or use of the Library is restricted in certain countries either by patents or by copyrighted interfaces, the original copyright holder who places the Library under this License may add an explicit geographical distribution limitation excluding those countries, so that distribution is permitted only in or among countries not thus excluded. In such case, this License incorporates the limitation as if written in the body of this License.

13. The Free Software Foundation may publish revised and/or new versions of the Lesser General Public License from time to time. Such new versions will be similar in spirit to the present version, but may differ in detail to address new problems or concerns.

Each version is given a distinguishing version number. If the Library specifies a version number of this License which applies to it and "any later version", you have the option of following the terms and conditions either of that version or of any later version published by the Free Software Foundation. If the Library does not specify a license version number, you may choose any version ever published by the Free Software Foundation.

14. If you wish to incorporate parts of the Library into other free programs whose distribution conditions are incompatible with these, write to the author to ask for permission. For software which is copyrighted by the Free Software Foundation, write to the Free Software Foundation; we sometimes make exceptions for this. Our decision will be guided by the two goals of preserving the free status of all derivatives of our free software and of promoting the sharing and reuse of software generally.

NO WARRANTY

15. BECAUSE THE LIBRARY IS LICENSED FREE OF CHARGE, THERE IS NO WARRANTY FOR THE LIBRARY, TO THE EXTENT PERMITTED BY APPLICABLE LAW. EXCEPT WHEN OTHERWISE STATED IN WRITING THE COPYRIGHT HOLDERS AND/OR OTHER PARTIES PROVIDE THE LIBRARY "AS IS" WITHOUT WARRANTY OF ANY KIND, EITHER EXPRESSED OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE. THE ENTIRE RISK AS TO THE QUALITY AND PERFORMANCE OF THE LIBRARY IS WITH YOU. SHOULD THE LIBRARY PROVE DEFECTIVE, YOU ASSUME THE COST OF ALL NECESSARY SERVICING, REPAIR OR CORRECTION.

16. IN NO EVENT UNLESS REQUIRED BY APPLICABLE LAW OR AGREED TO IN WRITING WILL ANY COPYRIGHT HOLDER, OR ANY OTHER PARTY WHO MAY MODIFY AND/OR REDISTRIBUTE THE LIBRARY AS PERMITTED ABOVE, BE LIABLE TO YOU FOR DAMAGES, INCLUDING ANY GENERAL, SPECIAL, INCIDENTAL OR CONSEQUENTIAL DAMAGES ARISING OUT OF THE USE OR INABILITY TO USE THE LIBRARY (INCLUDING BUT NOT LIMITED TO LOSS OF DATA OR DATA BEING RENDERED INACCURATE OR LOSSES SUSTAINED BY YOU OR THIRD PARTIES OR A FAILURE OF THE LIBRARY TO OPERATE WITH ANY OTHER SOFTWARE), EVEN IF SUCH HOLDER OR OTHER PARTY HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.

END OF TERMS AND CONDITIONS

How to Apply These Terms to Your New Libraries

If you develop a new library, and you want it to be of the greatest possible use to the public, we recommend making it free software that everyone can redistribute and change. You can do so by permitting redistribution under these terms (or, alternatively, under the terms of the ordinary General Public License).

To apply these terms, attach the following notices to the library. It is safest to attach them to the start of each source file to most effectively convey the exclusion of warranty; and each file should have at least the "copyright" line and a pointer to where the full notice is found.

     one line to give the library's name and an idea of what it does.
     Copyright (C) year  name of author

     This library is free software; you can redistribute it and/or modify it under the terms of the GNU Lesser General Public License as published by the Free Software Foundation; either version 2.1 of the License, or (at your option) any later version.

     This library is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more details.

     You should have received a copy of the GNU Lesser General Public License along with this library; if not, write to the Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA Also add information on how to contact you by electronic and paper mail.

You should also get your employer (if you work as a programmer) or your school, if any, to sign a "copyright disclaimer" for the library, if necessary. Here is a sample; alter the names:

Yoyodyne, Inc., hereby disclaims all copyright interest in
the library `Frob' (a library for tweaking knobs) written
by James Random Hacker.

signature of Ty Coon, 1 April 1990
Ty Coon, President of Vice
That's all there is to it!
```

### LLVM-exception

```
---- LLVM Exceptions to the Apache 2.0 License ----

   As an exception, if, as a result of your compiling your source code, portions
   of this Software are embedded into an Object form of such source code, you
   may redistribute such embedded portions in such Object form without complying
   with the conditions of Sections 4(a), 4(b) and 4(d) of the License.

   In addition, if you combine or link compiled forms of this Software with
   software that is licensed under the GPLv2 ("Combined Software") and if a
   court of competent jurisdiction determines that the patent provision (Section
   3), the indemnity provision (Section 9) or other Section of the License
   conflicts with the conditions of the GPLv2, you may retroactively and
   prospectively choose to deem waived or otherwise exclude such Section(s) of
   the License, but only in their entirety and only with respect to the Combined
   Software.
```

### MIT

```
MIT License

Copyright (c) <year> <copyright holders>

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial
portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### MIT-0

```
MIT No Attribution

Copyright <YEAR> <COPYRIGHT HOLDER>

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### MPL-2.0

```
Mozilla Public License Version 2.0
==================================

1. Definitions
--------------

1.1. "Contributor"
    means each individual or legal entity that creates, contributes to
    the creation of, or owns Covered Software.

1.2. "Contributor Version"
    means the combination of the Contributions of others (if any) used
    by a Contributor and that particular Contributor's Contribution.

1.3. "Contribution"
    means Covered Software of a particular Contributor.

1.4. "Covered Software"
    means Source Code Form to which the initial Contributor has attached
    the notice in Exhibit A, the Executable Form of such Source Code
    Form, and Modifications of such Source Code Form, in each case
    including portions thereof.

1.5. "Incompatible With Secondary Licenses"
    means

    (a) that the initial Contributor has attached the notice described
        in Exhibit B to the Covered Software; or

    (b) that the Covered Software was made available under the terms of
        version 1.1 or earlier of the License, but not also under the
        terms of a Secondary License.

1.6. "Executable Form"
    means any form of the work other than Source Code Form.

1.7. "Larger Work"
    means a work that combines Covered Software with other material, in 
    a separate file or files, that is not Covered Software.

1.8. "License"
    means this document.

1.9. "Licensable"
    means having the right to grant, to the maximum extent possible,
    whether at the time of the initial grant or subsequently, any and
    all of the rights conveyed by this License.

1.10. "Modifications"
    means any of the following:

    (a) any file in Source Code Form that results from an addition to,
        deletion from, or modification of the contents of Covered
        Software; or

    (b) any new file in Source Code Form that contains any Covered
        Software.

1.11. "Patent Claims" of a Contributor
    means any patent claim(s), including without limitation, method,
    process, and apparatus claims, in any patent Licensable by such
    Contributor that would be infringed, but for the grant of the
    License, by the making, using, selling, offering for sale, having
    made, import, or transfer of either its Contributions or its
    Contributor Version.

1.12. "Secondary License"
    means either the GNU General Public License, Version 2.0, the GNU
    Lesser General Public License, Version 2.1, the GNU Affero General
    Public License, Version 3.0, or any later versions of those
    licenses.

1.13. "Source Code Form"
    means the form of the work preferred for making modifications.

1.14. "You" (or "Your")
    means an individual or a legal entity exercising rights under this
    License. For legal entities, "You" includes any entity that
    controls, is controlled by, or is under common control with You. For
    purposes of this definition, "control" means (a) the power, direct
    or indirect, to cause the direction or management of such entity,
    whether by contract or otherwise, or (b) ownership of more than
    fifty percent (50%) of the outstanding shares or beneficial
    ownership of such entity.

2. License Grants and Conditions
--------------------------------

2.1. Grants

Each Contributor hereby grants You a world-wide, royalty-free,
non-exclusive license:

(a) under intellectual property rights (other than patent or trademark)
    Licensable by such Contributor to use, reproduce, make available,
    modify, display, perform, distribute, and otherwise exploit its
    Contributions, either on an unmodified basis, with Modifications, or
    as part of a Larger Work; and

(b) under Patent Claims of such Contributor to make, use, sell, offer
    for sale, have made, import, and otherwise transfer either its
    Contributions or its Contributor Version.

2.2. Effective Date

The licenses granted in Section 2.1 with respect to any Contribution
become effective for each Contribution on the date the Contributor first
distributes such Contribution.

2.3. Limitations on Grant Scope

The licenses granted in this Section 2 are the only rights granted under
this License. No additional rights or licenses will be implied from the
distribution or licensing of Covered Software under this License.
Notwithstanding Section 2.1(b) above, no patent license is granted by a
Contributor:

(a) for any code that a Contributor has removed from Covered Software;
    or

(b) for infringements caused by: (i) Your and any other third party's
    modifications of Covered Software, or (ii) the combination of its
    Contributions with other software (except as part of its Contributor
    Version); or

(c) under Patent Claims infringed by Covered Software in the absence of
    its Contributions.

This License does not grant any rights in the trademarks, service marks,
or logos of any Contributor (except as may be necessary to comply with
the notice requirements in Section 3.4).

2.4. Subsequent Licenses

No Contributor makes additional grants as a result of Your choice to
distribute the Covered Software under a subsequent version of this
License (see Section 10.2) or under the terms of a Secondary License (if
permitted under the terms of Section 3.3).

2.5. Representation

Each Contributor represents that the Contributor believes its
Contributions are its original creation(s) or it has sufficient rights
to grant the rights to its Contributions conveyed by this License.

2.6. Fair Use

This License is not intended to limit any rights You have under
applicable copyright doctrines of fair use, fair dealing, or other
equivalents.

2.7. Conditions

Sections 3.1, 3.2, 3.3, and 3.4 are conditions of the licenses granted
in Section 2.1.

3. Responsibilities
-------------------

3.1. Distribution of Source Form

All distribution of Covered Software in Source Code Form, including any
Modifications that You create or to which You contribute, must be under
the terms of this License. You must inform recipients that the Source
Code Form of the Covered Software is governed by the terms of this
License, and how they can obtain a copy of this License. You may not
attempt to alter or restrict the recipients' rights in the Source Code
Form.

3.2. Distribution of Executable Form

If You distribute Covered Software in Executable Form then:

(a) such Covered Software must also be made available in Source Code
    Form, as described in Section 3.1, and You must inform recipients of
    the Executable Form how they can obtain a copy of such Source Code
    Form by reasonable means in a timely manner, at a charge no more
    than the cost of distribution to the recipient; and

(b) You may distribute such Executable Form under the terms of this
    License, or sublicense it under different terms, provided that the
    license for the Executable Form does not attempt to limit or alter
    the recipients' rights in the Source Code Form under this License.

3.3. Distribution of a Larger Work

You may create and distribute a Larger Work under terms of Your choice,
provided that You also comply with the requirements of this License for
the Covered Software. If the Larger Work is a combination of Covered
Software with a work governed by one or more Secondary Licenses, and the
Covered Software is not Incompatible With Secondary Licenses, this
License permits You to additionally distribute such Covered Software
under the terms of such Secondary License(s), so that the recipient of
the Larger Work may, at their option, further distribute the Covered
Software under the terms of either this License or such Secondary
License(s).

3.4. Notices

You may not remove or alter the substance of any license notices
(including copyright notices, patent notices, disclaimers of warranty,
or limitations of liability) contained within the Source Code Form of
the Covered Software, except that You may alter any license notices to
the extent required to remedy known factual inaccuracies.

3.5. Application of Additional Terms

You may choose to offer, and to charge a fee for, warranty, support,
indemnity or liability obligations to one or more recipients of Covered
Software. However, You may do so only on Your own behalf, and not on
behalf of any Contributor. You must make it absolutely clear that any
such warranty, support, indemnity, or liability obligation is offered by
You alone, and You hereby agree to indemnify every Contributor for any
liability incurred by such Contributor as a result of warranty, support,
indemnity or liability terms You offer. You may include additional
disclaimers of warranty and limitations of liability specific to any
jurisdiction.

4. Inability to Comply Due to Statute or Regulation
---------------------------------------------------

If it is impossible for You to comply with any of the terms of this
License with respect to some or all of the Covered Software due to
statute, judicial order, or regulation then You must: (a) comply with
the terms of this License to the maximum extent possible; and (b)
describe the limitations and the code they affect. Such description must
be placed in a text file included with all distributions of the Covered
Software under this License. Except to the extent prohibited by statute
or regulation, such description must be sufficiently detailed for a
recipient of ordinary skill to be able to understand it.

5. Termination
--------------

5.1. The rights granted under this License will terminate automatically
if You fail to comply with any of its terms. However, if You become
compliant, then the rights granted under this License from a particular
Contributor are reinstated (a) provisionally, unless and until such
Contributor explicitly and finally terminates Your grants, and (b) on an
ongoing basis, if such Contributor fails to notify You of the
non-compliance by some reasonable means prior to 60 days after You have
come back into compliance. Moreover, Your grants from a particular
Contributor are reinstated on an ongoing basis if such Contributor
notifies You of the non-compliance by some reasonable means, this is the
first time You have received notice of non-compliance with this License
from such Contributor, and You become compliant prior to 30 days after
Your receipt of the notice.

5.2. If You initiate litigation against any entity by asserting a patent
infringement claim (excluding declaratory judgment actions,
counter-claims, and cross-claims) alleging that a Contributor Version
directly or indirectly infringes any patent, then the rights granted to
You by any and all Contributors for the Covered Software under Section
2.1 of this License shall terminate.

5.3. In the event of termination under Sections 5.1 or 5.2 above, all
end user license agreements (excluding distributors and resellers) which
have been validly granted by You or Your distributors under this License
prior to termination shall survive termination.

************************************************************************
*                                                                      *
*  6. Disclaimer of Warranty                                           *
*  -------------------------                                           *
*                                                                      *
*  Covered Software is provided under this License on an "as is"       *
*  basis, without warranty of any kind, either expressed, implied, or  *
*  statutory, including, without limitation, warranties that the       *
*  Covered Software is free of defects, merchantable, fit for a        *
*  particular purpose or non-infringing. The entire risk as to the     *
*  quality and performance of the Covered Software is with You.        *
*  Should any Covered Software prove defective in any respect, You     *
*  (not any Contributor) assume the cost of any necessary servicing,   *
*  repair, or correction. This disclaimer of warranty constitutes an   *
*  essential part of this License. No use of any Covered Software is   *
*  authorized under this License except under this disclaimer.         *
*                                                                      *
************************************************************************

************************************************************************
*                                                                      *
*  7. Limitation of Liability                                          *
*  --------------------------                                          *
*                                                                      *
*  Under no circumstances and under no legal theory, whether tort      *
*  (including negligence), contract, or otherwise, shall any           *
*  Contributor, or anyone who distributes Covered Software as          *
*  permitted above, be liable to You for any direct, indirect,         *
*  special, incidental, or consequential damages of any character      *
*  including, without limitation, damages for lost profits, loss of    *
*  goodwill, work stoppage, computer failure or malfunction, or any    *
*  and all other commercial damages or losses, even if such party      *
*  shall have been informed of the possibility of such damages. This   *
*  limitation of liability shall not apply to liability for death or   *
*  personal injury resulting from such party's negligence to the       *
*  extent applicable law prohibits such limitation. Some               *
*  jurisdictions do not allow the exclusion or limitation of           *
*  incidental or consequential damages, so this exclusion and          *
*  limitation may not apply to You.                                    *
*                                                                      *
************************************************************************

8. Litigation
-------------

Any litigation relating to this License may be brought only in the
courts of a jurisdiction where the defendant maintains its principal
place of business and such litigation shall be governed by laws of that
jurisdiction, without reference to its conflict-of-law provisions.
Nothing in this Section shall prevent a party's ability to bring
cross-claims or counter-claims.

9. Miscellaneous
----------------

This License represents the complete agreement concerning the subject
matter hereof. If any provision of this License is held to be
unenforceable, such provision shall be reformed only to the extent
necessary to make it enforceable. Any law or regulation which provides
that the language of a contract shall be construed against the drafter
shall not be used to construe this License against a Contributor.

10. Versions of the License
---------------------------

10.1. New Versions

Mozilla Foundation is the license steward. Except as provided in Section
10.3, no one other than the license steward has the right to modify or
publish new versions of this License. Each version will be given a
distinguishing version number.

10.2. Effect of New Versions

You may distribute the Covered Software under the terms of the version
of the License under which You originally received the Covered Software,
or under the terms of any subsequent version published by the license
steward.

10.3. Modified Versions

If you create software not governed by this License, and you want to
create a new license for such software, you may create and use a
modified version of this License if you rename the license and remove
any references to the name of the license steward (except to note that
such modified license differs from this License).

10.4. Distributing Source Code Form that is Incompatible With Secondary
Licenses

If You choose to distribute Source Code Form that is Incompatible With
Secondary Licenses under the terms of this version of the License, the
notice described in Exhibit B of this License must be attached.

Exhibit A - Source Code Form License Notice
-------------------------------------------

  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.

If it is not possible or desirable to put the notice in a particular
file, then You may include the notice in a location (such as a LICENSE
file in a relevant directory) where a recipient would be likely to look
for such a notice.

You may add additional accurate notices of copyright ownership.

Exhibit B - "Incompatible With Secondary Licenses" Notice
---------------------------------------------------------

  This Source Code Form is "Incompatible With Secondary Licenses", as
  defined by the Mozilla Public License, v. 2.0.
```

### OFL-1.1

```
SIL OPEN FONT LICENSE

Version 1.1 - 26 February 2007

PREAMBLE

The goals of the Open Font License (OFL) are to stimulate worldwide development of collaborative font projects, to support the font creation efforts of academic and linguistic communities, and to provide a free and open framework in which fonts may be shared and improved in partnership with others.

The OFL allows the licensed fonts to be used, studied, modified and redistributed freely as long as they are not sold by themselves. The fonts, including any derivative works, can be bundled, embedded, redistributed and/or sold with any software provided that any reserved names are not used by derivative works. The fonts and derivatives, however, cannot be released under any other type of license. The requirement for fonts to remain under this license does not apply to any document created using the fonts or their derivatives.

DEFINITIONS

"Font Software" refers to the set of files released by the Copyright Holder(s) under this license and clearly marked as such. This may include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the copyright statement(s).

"Original Version" refers to the collection of Font Software components as distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting, or substituting — in part or in whole — any of the components of the Original Version, by changing formats or by porting the Font Software to a new environment.

"Author" refers to any designer, engineer, programmer, technical writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS

Permission is hereby granted, free of charge, to any person obtaining a copy of the Font Software, to use, study, copy, merge, embed, modify, redistribute, and sell modified and unmodified copies of the Font Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components, in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled, redistributed and/or sold with any software, provided that each copy contains the above copyright notice and this license. These can be included either as stand-alone text files, human-readable headers or in the appropriate machine-readable metadata fields within text or binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font Name(s) unless explicit written permission is granted by the corresponding Copyright Holder. This restriction only applies to the primary font name as presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font Software shall not be used to promote, endorse or advertise any Modified Version, except to acknowledge the contribution(s) of the Copyright Holder(s) and the Author(s) or with their explicit written permission.

5) The Font Software, modified or unmodified, in part or in whole, must be distributed entirely under this license, and must not be distributed under any other license. The requirement for fonts to remain under this license does not apply to any document created using the Font Software.

TERMINATION

This license becomes null and void if any of the above conditions are not met.

DISCLAIMER

THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER DEALINGS IN THE FONT SOFTWARE.
```

### PSF-2.0

```
A. HISTORY OF THE SOFTWARE
==========================

Python was created in the early 1990s by Guido van Rossum at Stichting
Mathematisch Centrum (CWI, see https://www.cwi.nl) in the Netherlands
as a successor of a language called ABC.  Guido remains Python's
principal author, although it includes many contributions from others.

In 1995, Guido continued his work on Python at the Corporation for
National Research Initiatives (CNRI, see https://www.cnri.reston.va.us)
in Reston, Virginia where he released several versions of the
software.

In May 2000, Guido and the Python core development team moved to
BeOpen.com to form the BeOpen PythonLabs team.  In October of the same
year, the PythonLabs team moved to Digital Creations, which became
Zope Corporation.  In 2001, the Python Software Foundation (PSF, see
https://www.python.org/psf/) was formed, a non-profit organization
created specifically to own Python-related Intellectual Property.
Zope Corporation was a sponsoring member of the PSF.

All Python releases are Open Source (see https://opensource.org for
the Open Source Definition).  Historically, most, but not all, Python
releases have also been GPL-compatible; the table below summarizes
the various releases.

    Release         Derived     Year        Owner       GPL-
                    from                                compatible? (1)

    0.9.0 thru 1.2              1991-1995   CWI         yes
    1.3 thru 1.5.2  1.2         1995-1999   CNRI        yes
    1.6             1.5.2       2000        CNRI        no
    2.0             1.6         2000        BeOpen.com  no
    1.6.1           1.6         2001        CNRI        yes (2)
    2.1             2.0+1.6.1   2001        PSF         no
    2.0.1           2.0+1.6.1   2001        PSF         yes
    2.1.1           2.1+2.0.1   2001        PSF         yes
    2.1.2           2.1.1       2002        PSF         yes
    2.1.3           2.1.2       2002        PSF         yes
    2.2 and above   2.1.1       2001-now    PSF         yes

Footnotes:

(1) GPL-compatible doesn't mean that we're distributing Python under
    the GPL.  All Python licenses, unlike the GPL, let you distribute
    a modified version without making your changes open source.  The
    GPL-compatible licenses make it possible to combine Python with
    other software that is released under the GPL; the others don't.

(2) According to Richard Stallman, 1.6.1 is not GPL-compatible,
    because its license has a choice of law clause.  According to
    CNRI, however, Stallman's lawyer has told CNRI's lawyer that 1.6.1
    is "not incompatible" with the GPL.

Thanks to the many outside volunteers who have worked under Guido's
direction to make these releases possible.


B. TERMS AND CONDITIONS FOR ACCESSING OR OTHERWISE USING PYTHON
===============================================================

Python software and documentation are licensed under the
Python Software Foundation License Version 2.

Starting with Python 3.8.6, examples, recipes, and other code in
the documentation are dual licensed under the PSF License Version 2
and the Zero-Clause BSD license.

Some software incorporated into Python is under different licenses.
The licenses are listed with code falling under that license.


PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2
--------------------------------------------

1. This LICENSE AGREEMENT is between the Python Software Foundation
("PSF"), and the Individual or Organization ("Licensee") accessing and
otherwise using this software ("Python") in source or binary form and
its associated documentation.

2. Subject to the terms and conditions of this License Agreement, PSF hereby
grants Licensee a nonexclusive, royalty-free, world-wide license to reproduce,
analyze, test, perform and/or display publicly, prepare derivative works,
distribute, and otherwise use Python alone or in any derivative version,
provided, however, that PSF's License Agreement and PSF's notice of copyright,
i.e., "Copyright (c) 2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010,
2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023 Python Software Foundation;
All Rights Reserved" are retained in Python alone or in any derivative version
prepared by Licensee.

3. In the event Licensee prepares a derivative work that is based on
or incorporates Python or any part thereof, and wants to make
the derivative work available to others as provided herein, then
Licensee hereby agrees to include in any such work a brief summary of
the changes made to Python.

4. PSF is making Python available to Licensee on an "AS IS"
basis.  PSF MAKES NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR
IMPLIED.  BY WAY OF EXAMPLE, BUT NOT LIMITATION, PSF MAKES NO AND
DISCLAIMS ANY REPRESENTATION OR WARRANTY OF MERCHANTABILITY OR FITNESS
FOR ANY PARTICULAR PURPOSE OR THAT THE USE OF PYTHON WILL NOT
INFRINGE ANY THIRD PARTY RIGHTS.

5. PSF SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF PYTHON
FOR ANY INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS AS
A RESULT OF MODIFYING, DISTRIBUTING, OR OTHERWISE USING PYTHON,
OR ANY DERIVATIVE THEREOF, EVEN IF ADVISED OF THE POSSIBILITY THEREOF.

6. This License Agreement will automatically terminate upon a material
breach of its terms and conditions.

7. Nothing in this License Agreement shall be deemed to create any
relationship of agency, partnership, or joint venture between PSF and
Licensee.  This License Agreement does not grant permission to use PSF
trademarks or trade name in a trademark sense to endorse or promote
products or services of Licensee, or any third party.

8. By copying, installing or otherwise using Python, Licensee
agrees to be bound by the terms and conditions of this License
Agreement.


BEOPEN.COM LICENSE AGREEMENT FOR PYTHON 2.0
-------------------------------------------

BEOPEN PYTHON OPEN SOURCE LICENSE AGREEMENT VERSION 1

1. This LICENSE AGREEMENT is between BeOpen.com ("BeOpen"), having an
office at 160 Saratoga Avenue, Santa Clara, CA 95051, and the
Individual or Organization ("Licensee") accessing and otherwise using
this software in source or binary form and its associated
documentation ("the Software").

2. Subject to the terms and conditions of this BeOpen Python License
Agreement, BeOpen hereby grants Licensee a non-exclusive,
royalty-free, world-wide license to reproduce, analyze, test, perform
and/or display publicly, prepare derivative works, distribute, and
otherwise use the Software alone or in any derivative version,
provided, however, that the BeOpen Python License is retained in the
Software, alone or in any derivative version prepared by Licensee.

3. BeOpen is making the Software available to Licensee on an "AS IS"
basis.  BEOPEN MAKES NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR
IMPLIED.  BY WAY OF EXAMPLE, BUT NOT LIMITATION, BEOPEN MAKES NO AND
DISCLAIMS ANY REPRESENTATION OR WARRANTY OF MERCHANTABILITY OR FITNESS
FOR ANY PARTICULAR PURPOSE OR THAT THE USE OF THE SOFTWARE WILL NOT
INFRINGE ANY THIRD PARTY RIGHTS.

4. BEOPEN SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF THE
SOFTWARE FOR ANY INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS
AS A RESULT OF USING, MODIFYING OR DISTRIBUTING THE SOFTWARE, OR ANY
DERIVATIVE THEREOF, EVEN IF ADVISED OF THE POSSIBILITY THEREOF.

5. This License Agreement will automatically terminate upon a material
breach of its terms and conditions.

6. This License Agreement shall be governed by and interpreted in all
respects by the law of the State of California, excluding conflict of
law provisions.  Nothing in this License Agreement shall be deemed to
create any relationship of agency, partnership, or joint venture
between BeOpen and Licensee.  This License Agreement does not grant
permission to use BeOpen trademarks or trade names in a trademark
sense to endorse or promote products or services of Licensee, or any
third party.  As an exception, the "BeOpen Python" logos available at
http://www.pythonlabs.com/logos.html may be used according to the
permissions granted on that web page.

7. By copying, installing or otherwise using the software, Licensee
agrees to be bound by the terms and conditions of this License
Agreement.


CNRI LICENSE AGREEMENT FOR PYTHON 1.6.1
---------------------------------------

1. This LICENSE AGREEMENT is between the Corporation for National
Research Initiatives, having an office at 1895 Preston White Drive,
Reston, VA 20191 ("CNRI"), and the Individual or Organization
("Licensee") accessing and otherwise using Python 1.6.1 software in
source or binary form and its associated documentation.

2. Subject to the terms and conditions of this License Agreement, CNRI
hereby grants Licensee a nonexclusive, royalty-free, world-wide
license to reproduce, analyze, test, perform and/or display publicly,
prepare derivative works, distribute, and otherwise use Python 1.6.1
alone or in any derivative version, provided, however, that CNRI's
License Agreement and CNRI's notice of copyright, i.e., "Copyright (c)
1995-2001 Corporation for National Research Initiatives; All Rights
Reserved" are retained in Python 1.6.1 alone or in any derivative
version prepared by Licensee.  Alternately, in lieu of CNRI's License
Agreement, Licensee may substitute the following text (omitting the
quotes): "Python 1.6.1 is made available subject to the terms and
conditions in CNRI's License Agreement.  This Agreement together with
Python 1.6.1 may be located on the internet using the following
unique, persistent identifier (known as a handle): 1895.22/1013.  This
Agreement may also be obtained from a proxy server on the internet
using the following URL: http://hdl.handle.net/1895.22/1013".

3. In the event Licensee prepares a derivative work that is based on
or incorporates Python 1.6.1 or any part thereof, and wants to make
the derivative work available to others as provided herein, then
Licensee hereby agrees to include in any such work a brief summary of
the changes made to Python 1.6.1.

4. CNRI is making Python 1.6.1 available to Licensee on an "AS IS"
basis.  CNRI MAKES NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR
IMPLIED.  BY WAY OF EXAMPLE, BUT NOT LIMITATION, CNRI MAKES NO AND
DISCLAIMS ANY REPRESENTATION OR WARRANTY OF MERCHANTABILITY OR FITNESS
FOR ANY PARTICULAR PURPOSE OR THAT THE USE OF PYTHON 1.6.1 WILL NOT
INFRINGE ANY THIRD PARTY RIGHTS.

5. CNRI SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF PYTHON
1.6.1 FOR ANY INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS AS
A RESULT OF MODIFYING, DISTRIBUTING, OR OTHERWISE USING PYTHON 1.6.1,
OR ANY DERIVATIVE THEREOF, EVEN IF ADVISED OF THE POSSIBILITY THEREOF.

6. This License Agreement will automatically terminate upon a material
breach of its terms and conditions.

7. This License Agreement shall be governed by the federal
intellectual property law of the United States, including without
limitation the federal copyright law, and, to the extent such
U.S. federal law does not apply, by the law of the Commonwealth of
Virginia, excluding Virginia's conflict of law provisions.
Notwithstanding the foregoing, with regard to derivative works based
on Python 1.6.1 that incorporate non-separable material that was
previously distributed under the GNU General Public License (GPL), the
law of the Commonwealth of Virginia shall govern this License
Agreement only as to issues arising under or with respect to
Paragraphs 4, 5, and 7 of this License Agreement.  Nothing in this
License Agreement shall be deemed to create any relationship of
agency, partnership, or joint venture between CNRI and Licensee.  This
License Agreement does not grant permission to use CNRI trademarks or
trade name in a trademark sense to endorse or promote products or
services of Licensee, or any third party.

8. By clicking on the "ACCEPT" button where indicated, or by copying,
installing or otherwise using Python 1.6.1, Licensee agrees to be
bound by the terms and conditions of this License Agreement.

        ACCEPT


CWI LICENSE AGREEMENT FOR PYTHON 0.9.0 THROUGH 1.2
--------------------------------------------------

Copyright (c) 1991 - 1995, Stichting Mathematisch Centrum Amsterdam,
The Netherlands.  All rights reserved.

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose and without fee is hereby granted,
provided that the above copyright notice appear in all copies and that
both that copyright notice and this permission notice appear in
supporting documentation, and that the name of Stichting Mathematisch
Centrum or CWI not be used in advertising or publicity pertaining to
distribution of the software without specific, written prior
permission.

STICHTING MATHEMATISCH CENTRUM DISCLAIMS ALL WARRANTIES WITH REGARD TO
THIS SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS, IN NO EVENT SHALL STICHTING MATHEMATISCH CENTRUM BE LIABLE
FOR ANY SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

ZERO-CLAUSE BSD LICENSE FOR CODE IN THE PYTHON DOCUMENTATION
----------------------------------------------------------------------

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.



Additional Conditions for this Windows binary build
---------------------------------------------------

This program is linked with and uses Microsoft Distributable Code,
copyrighted by Microsoft Corporation. The Microsoft Distributable Code
is embedded in each .exe, .dll and .pyd file as a result of running
the code through a linker.

If you further distribute programs that include the Microsoft
Distributable Code, you must comply with the restrictions on
distribution specified by Microsoft. In particular, you must require
distributors and external end users to agree to terms that protect the
Microsoft Distributable Code at least as much as Microsoft's own
requirements for the Distributable Code. See Microsoft's documentation
(included in its developer tools and on its website at microsoft.com)
for specific details.

Redistribution of the Windows binary build of the Python interpreter
complies with this agreement, provided that you do not:

- alter any copyright, trademark or patent notice in Microsoft's
Distributable Code;

- use Microsoft's trademarks in your programs' names or in a way that
suggests your programs come from or are endorsed by Microsoft;

- distribute Microsoft's Distributable Code to run on a platform other
than Microsoft operating systems, run-time technologies or application
platforms; or

- include Microsoft Distributable Code in malicious, deceptive or
unlawful programs.

These restrictions apply only to the Microsoft Distributable Code as
defined above, not to Python itself or any programs running on the
Python interpreter. The redistribution of the Python interpreter and
libraries is governed by the Python Software License included with this
file, or by other licenses as marked.



--------------------------------------------------------------------------

This program, "bzip2", the associated library "libbzip2", and all
documentation, are copyright (C) 1996-2019 Julian R Seward.  All
rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:

1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

2. The origin of this software must not be misrepresented; you must 
   not claim that you wrote the original software.  If you use this 
   software in a product, an acknowledgment in the product 
   documentation would be appreciated but is not required.

3. Altered source versions must be plainly marked as such, and must
   not be misrepresented as being the original software.

4. The name of the author may not be used to endorse or promote 
   products derived from this software without specific prior written 
   permission.

THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS
OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

Julian Seward, jseward@acm.org
bzip2/libbzip2 version 1.0.8 of 13 July 2019

--------------------------------------------------------------------------

This software is copyrighted by the Regents of the University of
California, Sun Microsystems, Inc., Scriptics Corporation, ActiveState
Corporation and other parties.  The following terms apply to all files
associated with the software unless explicitly disclaimed in
individual files.

The authors hereby grant permission to use, copy, modify, distribute,
and license this software and its documentation for any purpose, provided
that existing copyright notices are retained in all copies and that this
notice is included verbatim in any distributions. No written agreement,
license, or royalty fee is required for any of the authorized uses.
Modifications to this software may be copyrighted by their authors
and need not follow the licensing terms described here, provided that
the new terms are clearly indicated on the first page of each file where
they apply.

IN NO EVENT SHALL THE AUTHORS OR DISTRIBUTORS BE LIABLE TO ANY PARTY
FOR DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES
ARISING OUT OF THE USE OF THIS SOFTWARE, ITS DOCUMENTATION, OR ANY
DERIVATIVES THEREOF, EVEN IF THE AUTHORS HAVE BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

THE AUTHORS AND DISTRIBUTORS SPECIFICALLY DISCLAIM ANY WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT.  THIS SOFTWARE
IS PROVIDED ON AN "AS IS" BASIS, AND THE AUTHORS AND DISTRIBUTORS HAVE
NO OBLIGATION TO PROVIDE MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR
MODIFICATIONS.

GOVERNMENT USE: If you are acquiring this software on behalf of the
U.S. government, the Government shall have only "Restricted Rights"
in the software and related documentation as defined in the Federal
Acquisition Regulations (FARs) in Clause 52.227.19 (c) (2).  If you
are acquiring the software on behalf of the Department of Defense, the
software shall be classified as "Commercial Computer Software" and the
Government shall have only "Restricted Rights" as defined in Clause
252.227-7014 (b) (3) of DFARs.  Notwithstanding the foregoing, the
authors grant the U.S. Government and others acting in its behalf
permission to use and distribute the software in accordance with the
terms specified in this license.

This software is copyrighted by the Regents of the University of
California, Sun Microsystems, Inc., Scriptics Corporation, ActiveState
Corporation, Apple Inc. and other parties.  The following terms apply to
all files associated with the software unless explicitly disclaimed in
individual files.

The authors hereby grant permission to use, copy, modify, distribute,
and license this software and its documentation for any purpose, provided
that existing copyright notices are retained in all copies and that this
notice is included verbatim in any distributions. No written agreement,
license, or royalty fee is required for any of the authorized uses.
Modifications to this software may be copyrighted by their authors
and need not follow the licensing terms described here, provided that
the new terms are clearly indicated on the first page of each file where
they apply.

IN NO EVENT SHALL THE AUTHORS OR DISTRIBUTORS BE LIABLE TO ANY PARTY
FOR DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES
ARISING OUT OF THE USE OF THIS SOFTWARE, ITS DOCUMENTATION, OR ANY
DERIVATIVES THEREOF, EVEN IF THE AUTHORS HAVE BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

THE AUTHORS AND DISTRIBUTORS SPECIFICALLY DISCLAIM ANY WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT.  THIS SOFTWARE
IS PROVIDED ON AN "AS IS" BASIS, AND THE AUTHORS AND DISTRIBUTORS HAVE
NO OBLIGATION TO PROVIDE MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR
MODIFICATIONS.

GOVERNMENT USE: If you are acquiring this software on behalf of the
U.S. government, the Government shall have only "Restricted Rights"
in the software and related documentation as defined in the Federal
Acquisition Regulations (FARs) in Clause 52.227.19 (c) (2).  If you
are acquiring the software on behalf of the Department of Defense, the
software shall be classified as "Commercial Computer Software" and the
Government shall have only "Restricted Rights" as defined in Clause
252.227-7013 (b) (3) of DFARs.  Notwithstanding the foregoing, the
authors grant the U.S. Government and others acting in its behalf
permission to use and distribute the software in accordance with the
terms specified in this license.

Copyright (c) 1993-1999 Ioi Kim Lam.
Copyright (c) 2000-2001 Tix Project Group.
Copyright (c) 2004 ActiveState

This software is copyrighted by the above entities
and other parties.  The following terms apply to all files associated
with the software unless explicitly disclaimed in individual files.

The authors hereby grant permission to use, copy, modify, distribute,
and license this software and its documentation for any purpose, provided
that existing copyright notices are retained in all copies and that this
notice is included verbatim in any distributions. No written agreement,
license, or royalty fee is required for any of the authorized uses.
Modifications to this software may be copyrighted by their authors
and need not follow the licensing terms described here, provided that
the new terms are clearly indicated on the first page of each file where
they apply.

IN NO EVENT SHALL THE AUTHORS OR DISTRIBUTORS BE LIABLE TO ANY PARTY
FOR DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES
ARISING OUT OF THE USE OF THIS SOFTWARE, ITS DOCUMENTATION, OR ANY
DERIVATIVES THEREOF, EVEN IF THE AUTHORS HAVE BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

THE AUTHORS AND DISTRIBUTORS SPECIFICALLY DISCLAIM ANY WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT.  THIS SOFTWARE
IS PROVIDED ON AN "AS IS" BASIS, AND THE AUTHORS AND DISTRIBUTORS HAVE
NO OBLIGATION TO PROVIDE MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR
MODIFICATIONS.

GOVERNMENT USE: If you are acquiring this software on behalf of the
U.S. government, the Government shall have only "Restricted Rights"
in the software and related documentation as defined in the Federal 
Acquisition Regulations (FARs) in Clause 52.227.19 (c) (2).  If you
are acquiring the software on behalf of the Department of Defense, the
software shall be classified as "Commercial Computer Software" and the
Government shall have only "Restricted Rights" as defined in Clause
252.227-7013 (c) (1) of DFARs.  Notwithstanding the foregoing, the
authors grant the U.S. Government and others acting in its behalf
permission to use and distribute the software in accordance with the
terms specified in this license. 

----------------------------------------------------------------------

Parts of this software are based on the Tcl/Tk software copyrighted by
the Regents of the University of California, Sun Microsystems, Inc.,
and other parties. The original license terms of the Tcl/Tk software
distribution is included in the file docs/license.tcltk.

Parts of this software are based on the HTML Library software
copyrighted by Sun Microsystems, Inc. The original license terms of
the HTML Library software distribution is included in the file
docs/license.html_lib.
```

### Unicode-3.0

```
UNICODE LICENSE V3

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2023 Unicode, Inc.

NOTICE TO USER: Carefully read the following legal agreement. BY
DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

Permission is hereby granted, free of charge, to any person obtaining a
copy of data files and any associated documentation (the "Data Files") or
software and any associated documentation (the "Software") to deal in the
Data Files or Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, and/or sell
copies of the Data Files or Software, and to permit persons to whom the
Data Files or Software are furnished to do so, provided that either (a)
this copyright and permission notice appear with all copies of the Data
Files or Software, or (b) this copyright and permission notice appear in
associated Documentation.

THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
THIRD PARTY RIGHTS.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall
not be used in advertising or otherwise to promote the sale, use or other
dealings in these Data Files or Software without prior written
authorization of the copyright holder.
```

### Unlicense

```
This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or distribute this software, either in source code form or as a compiled binary, for any purpose, commercial or non-commercial, and by any means.

In jurisdictions that recognize copyright laws, the author or authors of this software dedicate any and all copyright interest in the software to the public domain. We make this dedication for the benefit of the public at large and to the detriment of our heirs and
successors. We intend this dedication to be an overt act of relinquishment in perpetuity of all present and future rights to this software under copyright law.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to <http://unlicense.org/>
```

### Zlib

```
zlib License

This software is provided 'as-is', without any express or implied warranty.  In no event will the authors be held liable for any damages arising from the use of this software.

Permission is granted to anyone to use this software for any purpose, including commercial applications, and to alter it and redistribute it freely, subject to the following restrictions:

     1. The origin of this software must not be misrepresented; you must not claim that you wrote the original software. If you use this software in a product, an acknowledgment in the product documentation would be appreciated but is not required.

     2. Altered source versions must be plainly marked as such, and must not be misrepresented as being the original software.

     3. This notice may not be removed or altered from any source distribution.
```
