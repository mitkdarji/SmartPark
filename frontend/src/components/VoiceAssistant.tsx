/**
 * Voice + chat assistant.
 *
 * Speech recognition and synthesis run in the browser via the Web Speech API:
 * lowest latency, no audio ever leaves the device, and nothing to install. The
 * server-side Whisper path exists for kiosks and telephony; this component only
 * ever sends text.
 *
 * Mutating tools come back as a `pending_confirmation`, which is rendered as an
 * explicit approve/decline prompt. The model can propose spending money; only
 * the person can authorise it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { AgentReply } from '../lib/types'
import { Badge, Spinner } from './ui'

interface Message {
  role: 'user' | 'assistant'
  text: string
  meta?: { fastPath?: boolean; latency?: number; tools?: string[]; degraded?: boolean }
}

// Vendor-prefixed in Chrome and Safari; absent in Firefox.
type SpeechRecognitionLike = {
  continuous: boolean
  interimResults: boolean
  lang: string
  start: () => void
  stop: () => void
  onresult: ((event: any) => void) | null
  onerror: ((event: any) => void) | null
  onend: (() => void) | null
}

function getRecognition(): SpeechRecognitionLike | null {
  const w = window as any
  const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition
  if (!Ctor) return null
  const recognition: SpeechRecognitionLike = new Ctor()
  recognition.continuous = false
  recognition.interimResults = false
  recognition.lang = 'en-IN'
  return recognition
}

const DRIVER_PROMPTS = [
  'Where is my car?',
  'How much do I owe?',
  "What's my wallet balance?",
  'Give me directions to my slot',
]
const OWNER_PROMPTS = [
  'How is the facility running right now?',
  'Revenue for the last 7 days',
  'What will occupancy be in 3 hours?',
  'Any open anomalies?',
]

export function VoiceAssistant({
  facilityId, isOwner = false, compact = false,
}: { facilityId?: number | null; isOwner?: boolean; compact?: boolean }) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [listening, setListening] = useState(false)
  const [speak, setSpeak] = useState(false)
  const [conversationId, setConversationId] = useState<number | null>(null)
  const [pending, setPending] = useState<AgentReply['pending_confirmation']>(null)
  const [error, setError] = useState<string | null>(null)

  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const speechSupported = typeof window !== 'undefined' && !!getRecognition()

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])

  const say = useCallback((text: string) => {
    if (!speak || !('speechSynthesis' in window)) return
    window.speechSynthesis.cancel()
    const utterance = new SpeechSynthesisUtterance(text)
    utterance.lang = 'en-IN'
    utterance.rate = 1.02
    window.speechSynthesis.speak(utterance)
  }, [speak])

  const send = useCallback(
    async (text: string, confirm?: { tool: string; args: Record<string, unknown> }) => {
      const trimmed = text.trim()
      if (!trimmed && !confirm) return

      setError(null)
      setPending(null)
      if (trimmed) setMessages((prev) => [...prev, { role: 'user', text: trimmed }])
      setInput('')
      setBusy(true)

      try {
        // The voice endpoint short-circuits common questions without a model
        // call; anything it cannot classify falls through to the full agent.
        if (!confirm && !isOwner) {
          const voice = await api.voiceConverse({
            text: trimmed, conversation_id: conversationId, facility_id: facilityId ?? null,
          })
          setConversationId(voice.conversation_id ?? conversationId)
          setMessages((prev) => [
            ...prev,
            {
              role: 'assistant', text: voice.reply,
              meta: { fastPath: voice.fast_path, latency: voice.latency_ms, degraded: voice.degraded },
            },
          ])
          say(voice.speakable || voice.reply)
          return
        }

        const reply = await api.agentChat({
          message: trimmed || 'Please proceed.',
          conversation_id: conversationId,
          facility_id: facilityId ?? null,
          confirm_tool: confirm?.tool,
          confirm_arguments: confirm?.args,
        })
        setConversationId(reply.conversation_id)
        setPending(reply.pending_confirmation)
        setMessages((prev) => [
          ...prev,
          {
            role: 'assistant', text: reply.reply,
            meta: {
              latency: reply.latency_ms,
              tools: reply.tool_calls.map((call) => call.tool),
              degraded: reply.degraded,
            },
          },
        ])
        say(reply.speakable || reply.reply)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'The assistant is unavailable.')
      } finally {
        setBusy(false)
      }
    },
    [conversationId, facilityId, isOwner, say],
  )

  const toggleListening = () => {
    if (listening) {
      recognitionRef.current?.stop()
      setListening(false)
      return
    }
    const recognition = getRecognition()
    if (!recognition) {
      setError('This browser has no speech recognition. Type your question instead.')
      return
    }
    recognitionRef.current = recognition
    recognition.onresult = (event: any) => {
      const transcript = event.results?.[0]?.[0]?.transcript ?? ''
      if (transcript) void send(transcript)
    }
    recognition.onerror = () => {
      setListening(false)
      setError('Could not hear that. Check the microphone permission and try again.')
    }
    recognition.onend = () => setListening(false)
    setListening(true)
    setSpeak(true)         // speaking back is the point of speaking to it
    recognition.start()
  }

  const prompts = isOwner ? OWNER_PROMPTS : DRIVER_PROMPTS

  return (
    <div className="flex h-full flex-col">
      <div
        ref={scrollRef}
        className={`flex-1 space-y-3 overflow-y-auto pr-1 ${compact ? 'max-h-64' : 'min-h-[220px]'}`}
      >
        {messages.length === 0 && (
          <div className="space-y-2">
            <p className="text-xs text-ink-400">
              Ask about {isOwner ? 'your facility' : 'your parking'} — by voice or in writing.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {prompts.map((prompt) => (
                <button
                  key={prompt}
                  onClick={() => void send(prompt)}
                  className="rounded-full border border-ink-700 px-2.5 py-1 text-[11px] text-ink-300 hover:border-brand-500 hover:text-white"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message, index) => (
          <div
            key={index}
            className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[85%] rounded-xl px-3 py-2 text-sm ${
                message.role === 'user'
                  ? 'bg-brand-600 text-white'
                  : 'border border-ink-800 bg-ink-900 text-ink-100'
              }`}
            >
              <p className="whitespace-pre-wrap">{message.text}</p>
              {message.meta && message.role === 'assistant' && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  {message.meta.fastPath && <Badge tone="good">instant</Badge>}
                  {message.meta.degraded && <Badge tone="warn">rule-based</Badge>}
                  {message.meta.tools?.map((tool) => (
                    <Badge key={tool} tone="info">{tool}</Badge>
                  ))}
                  {message.meta.latency !== undefined && (
                    <span className="text-[10px] text-ink-500">
                      {message.meta.latency.toFixed(0)} ms
                    </span>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}

        {busy && <Spinner label="Thinking…" />}
      </div>

      {pending && (
        <div className="mt-3 rounded-lg border border-signal-amber/40 bg-signal-amber/10 p-3">
          <p className="text-xs font-semibold text-signal-amber">Confirmation required</p>
          <p className="mt-1 text-xs text-ink-200">
            <code className="text-ink-100">{pending.tool}</code>{' '}
            {JSON.stringify(pending.arguments)}
          </p>
          <div className="mt-2 flex gap-2">
            <button
              className="btn-primary btn-sm"
              onClick={() => void send('', { tool: pending.tool, args: pending.arguments })}
            >
              Approve
            </button>
            <button className="btn-ghost btn-sm" onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && (
        <p className="mt-2 rounded-md border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-xs text-signal-red">
          {error}
        </p>
      )}

      <form
        className="mt-3 flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          void send(input)
        }}
      >
        <button
          type="button"
          onClick={toggleListening}
          disabled={!speechSupported}
          title={speechSupported ? 'Speak' : 'Speech recognition is unavailable in this browser'}
          className={`relative flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border transition-colors ${
            listening
              ? 'border-signal-red bg-signal-red/20 text-signal-red'
              : 'border-ink-700 text-ink-300 hover:text-white disabled:opacity-40'
          }`}
        >
          {listening && (
            <span className="absolute inset-0 animate-pulse-ring rounded-lg border border-signal-red" />
          )}
          🎙
        </button>
        <input
          className="input flex-1"
          placeholder={listening ? 'Listening…' : 'Ask a question…'}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          disabled={busy}
        />
        <button
          type="button"
          onClick={() => setSpeak((value) => !value)}
          title={speak ? 'Spoken replies on' : 'Spoken replies off'}
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border transition-colors ${
            speak ? 'border-brand-500 text-brand-400' : 'border-ink-700 text-ink-500 hover:text-white'
          }`}
        >
          {speak ? '🔊' : '🔇'}
        </button>
        <button type="submit" className="btn-primary" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  )
}
