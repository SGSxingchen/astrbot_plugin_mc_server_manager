// KubeJS 6 / Minecraft Forge 1.20.1
// Minimal RCON-pollable chat queue for AstrBot Minecraft Server Manager.
// No Java.loadClass here: keep it compatible with strict Rhino class filters.

const MCAI_MAX_QUEUE = 200
const MCAI_MAX_MESSAGE_CHARS = 1000
// Keep this in sync with the plugin's chat_prefix/chat_prefixes.
const MCAI_PREFIXES = ['!ai']

global.mcaiBridgeQueue = global.mcaiBridgeQueue || []
global.mcaiBridgeNextId = global.mcaiBridgeNextId || 1

function mcaiEnc(value) {
  return encodeURIComponent(String(value || ''))
}

function mcaiReply(source, text) {
  const msg = String(text || '')
  try {
    source.sendSystemMessage(Text.of(msg))
  } catch (e) {
    console.log(msg)
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

PlayerEvents.chat(event => {
  if (mcaiMatchesPrefix(event.message)) {
    mcaiQueueMessage(event.player.username, event.message)
  }
})

ServerEvents.commandRegistry(event => {
  const { commands: Commands, arguments: Arguments } = event
  event.register(
    Commands.literal('ai')
      .then(
        Commands.argument('message', Arguments.GREEDY_STRING.create(event))
          .executes(ctx => {
            const player = ctx.source.playerOrException
            const message = String(Arguments.GREEDY_STRING.getResult(ctx, 'message') || '').trim()
            if (message.length > 0) {
              mcaiQueueMessage(player.username, MCAI_PREFIXES[0] + ' ' + message)
              try {
                player.server.tell(Text.of('[AI] <' + player.username + '> ' + message))
              } catch (e) {
                player.tell(Text.of('[AI] ' + message))
              }
              return 1
            }
            return 0
          })
      )
  )

  event.register(
    Commands.literal('mcai_bridge')
      .requires(src => src.hasPermission(4))
      .then(
        Commands.literal('poll')
          .then(
            Commands.argument('limit', Arguments.INTEGER.create(event))
              .executes(ctx => {
                let limit = Arguments.INTEGER.getResult(ctx, 'limit')
                if (limit < 1) limit = 1
                if (limit > 100) limit = 100
                const lines = ['MCAI_QUEUE_V2']
                const items = global.mcaiBridgeQueue.slice(0, limit)
                if (items.length === 0) {
                  lines.push('empty')
                } else {
                  items.forEach(item => {
                    lines.push([item.id, mcaiEnc(item.player), mcaiEnc(item.message), String(item.ts || 0)].join('\t'))
                  })
                }
                mcaiReply(ctx.source, lines.join('\n'))
                return items.length
              })
          )
          .executes(ctx => {
            const lines = ['MCAI_QUEUE_V2']
            const items = global.mcaiBridgeQueue.slice(0, 20)
            if (items.length === 0) {
              lines.push('empty')
            } else {
              items.forEach(item => {
                lines.push([item.id, mcaiEnc(item.player), mcaiEnc(item.message), String(item.ts || 0)].join('\t'))
              })
            }
            mcaiReply(ctx.source, lines.join('\n'))
            return items.length
          })
      )
      .then(
        Commands.literal('ack')
          .then(
            Commands.argument('ids', Arguments.STRING.create(event))
              .executes(ctx => {
                const raw = String(Arguments.STRING.getResult(ctx, 'ids') || '')
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
