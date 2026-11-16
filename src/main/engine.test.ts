import { createServer } from 'node:net'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { engineEnv, enginePaths, engineSteps, freePort } from './engine'

describe('engine launcher', () => {
  it('from a source checkout, runs the engine through the developer’s uv', () => {
    const paths = enginePaths('C:\\proatc', 'C:\\res', 'C:\\data', false)
    expect(engineSteps(paths, 8765)).toEqual([
      { command: 'uv', args: ['run', '--project', 'C:\\proatc', 'proatc', 'serve', '--port', '8765'] }
    ])
    const env = engineEnv(paths, 'secret', { PATH: 'x' })
    expect(env).toMatchObject({ PATH: 'x', PROATC_TOKEN: 'secret', PYTHONUNBUFFERED: '1' })
    expect(env.PROATC_HOME).toBeUndefined()
    expect(env.UV_PROJECT_ENVIRONMENT).toBeUndefined()
  })

  it('installed, builds its own environment from the lock file and keeps data in the user folder', () => {
    const paths = enginePaths('ignored', 'C:\\res', 'C:\\data', true)
    const engineDir = join('C:\\res', 'engine')
    expect(paths.uv).toBe(join('C:\\res', 'bin', process.platform === 'win32' ? 'uv.exe' : 'uv'))
    const [sync, serve] = engineSteps(paths, 9000)
    expect(sync.args).toEqual(['sync', '--frozen', '--no-dev', '--no-editable', '--project', engineDir])
    expect(sync.note).toMatch(/first run/)
    expect(serve.args).toEqual(['run', '--no-sync', '--project', engineDir, 'proatc', 'serve', '--port', '9000'])
    expect(engineEnv(paths, 't', {})).toMatchObject({
      PROATC_HOME: join('C:\\data', 'engine'),
      UV_PROJECT_ENVIRONMENT: join('C:\\data', 'engine-venv')
    })
  })

  it('can leave out the GPU libraries', () => {
    const [sync] = engineSteps(enginePaths('', 'C:\\res', 'C:\\data', true), 9000, true)
    expect(sync.args.slice(-4)).toEqual([
      '--no-install-package',
      'nvidia-cublas-cu12',
      '--no-install-package',
      'nvidia-cudnn-cu12'
    ])
  })

  it('finds a port that can be listened on', async () => {
    const port = await freePort()
    expect(port).toBeGreaterThan(1023)
    await new Promise<void>((resolve, reject) => {
      const server = createServer()
      server.once('error', reject)
      server.listen(port, '127.0.0.1', () => server.close(() => resolve()))
    })
  })
})
