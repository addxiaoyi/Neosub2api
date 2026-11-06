/**
 * 首次启动向导的跨组件约定。
 *
 * 向导状态以库内 `/api/onboarding` 为准；sessionStorage 只用来「本次会话别再自动弹」，
 * 绝不承载「已完成」这个事实——否则换个浏览器就会重来一遍。
 */

export const ONBOARDING_OPEN_EVENT = 'unified:open-onboarding';
export const ONBOARDING_UPDATED_EVENT = 'unified:onboarding-updated';
export const ONBOARDING_SNOOZE_KEY = 'wb-onboarding-snoozed';

/** 向导签发密钥的固定名字：密钥页据此标出来源，改名即视为用户接管 */
export const WIZARD_KEY_NAME = 'first-run-wizard';

export const STEP_IDS = ['account', 'provider', 'credential', 'selfcheck', 'smoke', 'key'] as const;
export type OnboardingStepId = (typeof STEP_IDS)[number];

export function openOnboarding(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new Event(ONBOARDING_OPEN_EVENT));
}

export function notifyOnboardingUpdated(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new Event(ONBOARDING_UPDATED_EVENT));
}

export function snoozeOnboarding(): void {
  try {
    window.sessionStorage.setItem(ONBOARDING_SNOOZE_KEY, '1');
  } catch {
    /* 隐私模式下不可用；下次进页面仍会弹，不影响库内状态 */
  }
}

export function isOnboardingSnoozed(): boolean {
  try {
    return window.sessionStorage.getItem(ONBOARDING_SNOOZE_KEY) === '1';
  } catch {
    return false;
  }
}

/** 缺失项文案键；向导跳过与首页待办共用，避免两处各说一套 */
export const MISSING_KEY: Record<OnboardingStepId, string> = {
  account: 'onboarding.missing.account',
  provider: 'onboarding.missing.provider',
  credential: 'onboarding.missing.credential',
  selfcheck: 'onboarding.missing.selfcheck',
  smoke: 'onboarding.missing.smoke',
  key: 'onboarding.missing.key',
};
