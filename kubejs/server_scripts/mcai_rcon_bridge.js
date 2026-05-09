// KubeJS 6 / Minecraft Forge 1.20.1
// Minimal RCON-pollable chat queue for AstrBot Minecraft Server Manager.
// Install: copy this file to <server>/kubejs/server_scripts/mcai_rcon_bridge.js,
// then restart the server or run `/reload` after KubeJS is loaded.

const Component = Java.loadClass('net.minecraft.network.chat.Component')
const Base64 = Java.loadClass('java.util.Base64')
const StandardCharsets = Java.loadClass('java.nio.charset.StandardCharsets')
const JavaString = Java.loadClass('java.lang.String')
const IntegerArgumentType = Java.loadClass('com.mojang.brigadier.arguments.IntegerArgumentType')
const StringArgumentType = Java.loadClass('com.mojang.brigadier.arguments.StringArgumentType')

const MCAI_MAX_QUEUE = 200
const MCAI_MAX_MESSAGE_CHARS = 1000
// Keep this in sync with the plugin's chat_prefix/chat_prefixes. Only matching
// player messages enter the RCON-pollable queue.
const MCAI_PREFIXES = ['!ai']

global.mcaiBridgeQueue = global.mcaiBridgeQueue || []
global.mcaiBridgeNextId = global.mcaiBridgeNextId || 1

function mcaiB64(value) {
  return Base64.getEncoder().encodeToString(new JavaString(String(value || '')).getBytes(StandardCharsets.UTF_8))
}

function mcaiReply(source, text) {
  const component = Component.literal(String(text || ''))
  try {
    source.sendSuccess(() => component, false)
  } catch (e) {
    source.sendSystemMessage(component)
  }
}

function mcaiQueueMessage(player, message) {
  const cleanPlayer = String(player || '').substring(0, 80)
  const cleanMessage = String(message || '').replace(/[\u0000-\u001f\u007f]/g, '').substring(0, MCAI_MAX_MESSAGE_CHARS)
  if (!cleanPlayer || !cleanMessage) return
  const id = String(global.mcaiBridgeNextId++)
  global.mcaiBridgeQueue.push({ id: id, player: cleanPlayer, message: cleanMessage, ts: Date.now() })
  while (global.mcaiBridgeQueue.length > MCAI_MAX_QUEUE) global.mcaiBridgeQueue.shift()
}

function mcaiMatchesPrefix(message) {
  const text = String(message || '').trim()
  return MCAI_PREFIXES.some(prefix => text === prefix || text.startsWith(prefix + ' '))
}

// Capture prefixed player chat into an in-memory queue. The AstrBot plugin
// validates the prefix again on the RCON side as a safety check.
PlayerEvents.chat(event => {
  if (mcaiMatchesPrefix(event.message)) {
    mcaiQueueMessage(event.player.username, event.message)
  }
})

ServerEvents.commandRegistry(event => {
  const Commands = event.commands
  event.register(
    Commands.literal('mcai_bridge')
      .requires(source => source.hasPermission(4))
      .then(
        Commands.literal('poll')
          .then(
            Commands.argument('limit', IntegerArgumentType.integer(1, 100))
              .executes(ctx => {
                const limit = IntegerArgumentType.getInteger(ctx, 'limit')
                const lines = ['MCAI_QUEUE_V1']
                const items = global.mcaiBridgeQueue.slice(0, limit)
                if (items.length === 0) {
                  lines.push('empty')
                } else {
                  items.forEach(item => {
                    lines.push([item.id, mcaiB64(item.player), mcaiB64(item.message), String(item.ts || 0)].join('\t'))
                  })
                }
                mcaiReply(ctx.source, lines.join('\n'))
                return items.length
              })
          )
          .executes(ctx => {
            const lines = ['MCAI_QUEUE_V1']
            const items = global.mcaiBridgeQueue.slice(0, 20)
            if (items.length === 0) {
              lines.push('empty')
            } else {
              items.forEach(item => {
                lines.push([item.id, mcaiB64(item.player), mcaiB64(item.message), String(item.ts || 0)].join('\t'))
              })
            }
            mcaiReply(ctx.source, lines.join('\n'))
            return items.length
          })
      )
      .then(
        Commands.literal('ack')
          .then(
            Commands.argument('ids', StringArgumentType.word())
              .executes(ctx => {
                const raw = StringArgumentType.getString(ctx, 'ids')
                const ids = new Set(raw.split(',').map(x => x.trim()).filter(x => x.length > 0))
                const before = global.mcaiBridgeQueue.length
                global.mcaiBridgeQueue = global.mcaiBridgeQueue.filter(item => !ids.has(String(item.id)))
                const removed = before - global.mcaiBridgeQueue.length
                mcaiReply(ctx.source, 'MCAI_ACK_V1 removed=' + removed)
                return removed
              })
          )
      )
      .then(
        Commands.literal('size')
          .executes(ctx => {
            mcaiReply(ctx.source, 'MCAI_SIZE_V1 size=' + global.mcaiBridgeQueue.length)
            return global.mcaiBridgeQueue.length
          })
      )
  )
})
