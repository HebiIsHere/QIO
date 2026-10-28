<script setup lang="ts">
/**
 * 关于与更新：版本、更新（沿用 UpdateCard）、许可证与第三方声明（离线内联）。
 *
 * 内容来源保持不变：LICENSE 与 THIRD-PARTY-NOTICES.md 是仓库根目录的唯一来源，
 * 由 Vite 在构建期内联（?raw），随应用分发、离线可读；更新与重启的决定方式不变。
 */
import UpdateCard from "../../../components/UpdateCard.vue";
import AdvGroup from "../shell/AdvGroup.vue";
import appPackage from "../../../../package.json";
import projectLicenseText from "../../../../../LICENSE?raw";
import thirdPartyNoticesText from "../../../../../THIRD-PARTY-NOTICES.md?raw";

const appVersion = String(appPackage.version ?? "");
</script>

<template>
  <section class="set-section" data-settings-section="about" data-testid="settings-about" aria-label="关于与更新">
    <header class="set-section__head">
      <h2>版本</h2>
      <p class="set-section__desc">
        模型 Key 全部由你自己提供（BYOK），应用没有自带 Key；数据留在本机（SQLite + 系统凭据库）。
        除更新检查和你自己发起的模型 / 搜索调用外，应用不主动上传内容。
      </p>
    </header>
    <dl class="set-raw" data-settings-key="about.version">
      <dt>应用</dt>
      <dd>QIO {{ appVersion }}</dd>
    </dl>

    <header class="set-section__head">
      <h2>更新</h2>
    </header>
    <div class="set-update" data-settings-key="about.update">
      <UpdateCard />
    </div>

    <header class="set-section__head">
      <h2>许可证</h2>
      <p class="set-section__desc">
        QIO 以 MIT 许可证开源：可以自由使用、修改、再分发（含商用），只需保留版权声明与许可声明。
        完整条款见仓库根目录的 LICENSE（本页展示的就是它）。
      </p>
    </header>
    <div data-settings-key="about.license">
      <AdvGroup id="about.license" label="查看 QIO 许可全文">
        <pre class="set-license" data-testid="qio-license">{{ projectLicenseText }}</pre>
      </AdvGroup>
    </div>

    <header class="set-section__head">
      <h2>第三方开源组件</h2>
      <p class="set-section__desc">
        QIO 随安装包分发第三方开源组件（Python 依赖、npm 包、Rust crate、内置 CPython 运行时、
        内置中文嵌入模型与中文字体）。完整声明随应用一起分发，无需联网即可查阅；
        覆盖率由仓库里的校验脚本保证，少一个组件就失败。
      </p>
    </header>
    <div data-settings-key="about.third-party">
      <AdvGroup id="about.third-party" label="查看第三方开源组件声明（生成物，请勿手工编辑）">
        <pre class="set-license" data-testid="third-party-notices">{{ thirdPartyNoticesText }}</pre>
      </AdvGroup>
    </div>
  </section>
</template>
