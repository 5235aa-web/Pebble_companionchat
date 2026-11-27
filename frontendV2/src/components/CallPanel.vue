<script setup lang="ts">
/**
 * Voice call panel.
 *
 * A bottom-centred bar rather than a floating orb: the call is a side channel to
 * the conversation, so it sits above the composer without covering the thread.
 * The waveform is one CSS variable driving forty bars. The reply itself lands in
 * the thread like any other message, so the panel carries state and controls
 * only — no second copy of the dialogue.
 */
import { computed, onBeforeUnmount } from 'vue'
import { useCallStore } from '@/stores/call'
import { useI18n } from '@/i18n'

const call = useCallStore()
const { t } = useI18n()

const phaseLabel = computed(() => {
  if (call.warming) return t('call.warming')
  switch (call.phase) {
    case 'listening':
      return t('call.listening')
    case 'transcribing':
      return t('call.transcribing')
    case 'thinking':
      return t('call.thinking')
    case 'speaking':
      return t('call.speaking')
    default:
      return ''
  }
})

const clock = computed(() => {
  const s = call.seconds
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
})

/** Level is a raw RMS; the bars need a curve that reads at conversational volume. */
const waveLevel = computed(() => String(Math.min(1, Math.sqrt(call.level) * 3.4)))

onBeforeUnmount(() => {
  if (call.active) call.hangup()
})
</script>

<template>
  <Transition name="cp">
    <div v-if="call.active" class="cp" :data-phase="call.phase">
      <div class="cp-head">
        <span class="cp-dot"></span>
        <span class="cp-phase">{{ phaseLabel }}</span>
        <span class="cp-time">{{ clock }}</span>
        <button
          v-if="call.phase === 'listening' && !call.warming"
          type="button"
          class="cp-done"
          @click="call.endTurn()"
        >{{ t('call.doneSpeaking') }}</button>
        <button
          v-else-if="call.phase === 'speaking'"
          type="button"
          class="cp-done"
          @click="call.interrupt()"
        >{{ t('call.interrupt') }}</button>
        <button class="cp-hang" :aria-label="t('call.hangup')" @click="call.hangup()">
          <svg viewBox="0 0 24 24">
            <path d="M4 14c4.5-4.5 11.5-4.5 16 0l-2.2 2.4a1.4 1.4 0 01-1.8.2l-1.9-1.4a1.6 1.6 0 01-.6-1.6l.2-1a11 11 0 00-4.4 0l.2 1a1.6 1.6 0 01-.6 1.6L6 16.6a1.4 1.4 0 01-1.8-.2z" />
          </svg>
        </button>
      </div>

      <div class="cp-wave" :style="{ '--lv': waveLevel }">
        <span v-for="i in 40" :key="i" class="cp-bar" :style="{ '--i': String(i) }"></span>
      </div>

      <div v-if="call.endKeyword" class="cp-hint">
        {{ t('call.endHint', { word: call.endKeyword }) }}
      </div>

      <div v-if="call.partial" class="cp-line">
        <span class="cp-line-tag">{{ t('call.you') }}</span>
        <span class="cp-line-text">{{ call.partial }}</span>
      </div>

      <div v-if="call.errMsg" class="cp-err">{{ call.errMsg }}</div>
    </div>
  </Transition>
</template>

<style scoped>
.cp {
  position: fixed;
  left: 50%;
  bottom: 104px;
  transform: translateX(-50%);
  z-index: 38;
  width: min(560px, calc(100vw - 40px));
  padding: 14px 18px 16px;
  background: var(--bg-2);
  border: 1px solid var(--line-2);
  box-shadow: 0 26px 70px rgb(var(--shadow-rgb) / 0.46);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.cp-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

/*
  Live transcript. Two lines then ellipsis: the panel is a status bar, and a long
  paragraph pushing the waveform around would make the bar jump every frame.
*/
/*
  The keyword reminder. Turn-taking is explicit now, so the bar has to say which
  word closes a sentence — otherwise the only way to learn it is to get stuck.
*/
.cp-hint {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-4);
  padding-top: 2px;
}

.cp-line {
  display: flex;
  align-items: baseline;
  gap: 9px;
  min-height: 19px;
  padding-top: 2px;
}

.cp-line-tag {
  flex: none;
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-4);
}

.cp-line-text {
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--ink-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.cp-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 3px rgb(var(--accent-rgb) / 0.16);
  animation: cpPulse 1.8s ease-in-out infinite;
}

.cp[data-phase='thinking'] .cp-dot {
  animation-duration: 0.9s;
}

.cp[data-phase='speaking'] .cp-dot {
  background: var(--ink-2);
  box-shadow: 0 0 0 3px rgb(var(--ink-rgb) / 0.12);
}

.cp-phase {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--ink-2);
}

.cp-time {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  color: var(--ink-4);
}

.cp-hang {
  flex: none;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid rgb(var(--ember-rgb) / 0.42);
  color: var(--ember);
  transition: background 0.2s ease, border-color 0.2s ease;
}

.cp-done {
  flex: none;
  height: 28px;
  padding: 0 10px;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  border: 1px solid rgb(var(--accent-rgb) / 0.45);
  color: var(--accent);
  transition: background 0.2s ease, border-color 0.2s ease;
}

.cp-done:hover {
  background: rgb(var(--accent-rgb) / 0.12);
  border-color: var(--accent);
}

.cp-hang:hover {
  background: rgb(var(--ember-rgb) / 0.12);
  border-color: rgb(var(--ember-rgb) / 0.7);
}

.cp-hang svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linejoin: round;
}

.cp-wave {
  display: flex;
  align-items: center;
  gap: 3px;
  height: 32px;
  padding: 0 1px;
}

.cp-bar {
  flex: 1;
  height: 100%;
  background: var(--accent);
  opacity: 0.5;
  transform-origin: center;
  transform: scaleY(calc(0.1 + var(--lv, 0) * 0.9));
  transition: transform 0.09s linear, opacity 0.25s ease;
  animation: cpBreath 1.5s ease-in-out infinite;
  animation-delay: calc(var(--i) * -0.07s);
}

.cp[data-phase='speaking'] .cp-bar {
  background: var(--ink-2);
  opacity: 0.42;
}

.cp-err {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--ember);
}

.cp-enter-active,
.cp-leave-active {
  transition: opacity 0.34s ease, transform 0.34s var(--ease);
}

.cp-enter-from,
.cp-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(14px);
}

@keyframes cpPulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.45;
    transform: scale(0.82);
  }
}

@keyframes cpBreath {
  0%,
  100% {
    opacity: 0.34;
  }
  50% {
    opacity: 0.62;
  }
}

@media (prefers-reduced-motion: reduce) {
  .cp-bar,
  .cp-dot {
    animation: none;
  }
}
</style>
