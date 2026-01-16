# test_solana_py_capabilities.py
import asyncio
import json
from typing import cast

from solana.rpc.websocket_api import connect, SolanaWsClientProtocol
from solders.pubkey import Pubkey

async def test_solana_py_capabilities():
    """测试solana-py的能力"""
    
    # 使用一个真实的测试账户（或使用Pubkey.default()）
    test_pubkey = Pubkey.default()  # 或者使用真实的pubkey
    
    ws_url = "wss://api.mainnet-beta.solana.com"  # 或使用devnet
    
    print("=" * 60)
    print("测试solana-py的能力")
    print("=" * 60)
    print(f"连接URL: {ws_url}")
    print(f"测试pubkey: {test_pubkey}")
    
    try:
        async for ws in connect(ws_url):
            ws = cast(SolanaWsClientProtocol, ws)
            
            msg = None
            subscription_id = None
            
            try:
            # 测试1.1: account_subscribe的返回值
            print("\n[测试1.1] 调用account_subscribe...")
            result = await ws.account_subscribe(
                test_pubkey,
                commitment="confirmed",
                encoding="base64",
            )
            
            print(f"返回类型: {type(result)}")
            print(f"返回值: {result}")
            if result is not None:
                print(f"返回值属性: {dir(result)}")
                
                # 检查是否有request_id属性
                if hasattr(result, 'request_id'):
                    print(f"✅ 有request_id属性: {result.request_id}")
                else:
                    print("❌ 没有request_id属性")
            else:
                print("⚠️ account_subscribe返回None（这是正常的，它不返回request_id）")
            
            # 测试1.2: 等待响应消息
            print("\n[测试1.2] 等待响应消息...")
            msg = await ws.recv()
            
            print(f"消息类型: {type(msg)}")
            print(f"消息长度: {len(msg) if hasattr(msg, '__len__') else 'N/A'}")
            
            if len(msg) > 0:
                print(f"msg[0]类型: {type(msg[0])}")
                print(f"msg[0]属性: {dir(msg[0])}")
                
                # 检查是否有id属性
                if hasattr(msg[0], 'id'):
                    print(f"✅ msg[0]有id属性: {msg[0].id}")
                else:
                    print("❌ msg[0]没有id属性")
                
                # 检查是否有result属性
                if hasattr(msg[0], 'result'):
                    print(f"✅ msg[0]有result属性: {msg[0].result}")
                    print(f"result类型: {type(msg[0].result)}")
                
                # 检查是否有subscription属性
                if hasattr(msg[0], 'subscription'):
                    print(f"✅ msg[0]有subscription属性: {msg[0].subscription}")
                
                # 尝试访问原始消息（如果有）
                if hasattr(msg[0], '__dict__'):
                    print(f"msg[0]的__dict__: {msg[0].__dict__}")
                
                # 尝试访问所有属性
                print(f"\nmsg[0]的所有属性值:")
                for attr in dir(msg[0]):
                    if not attr.startswith('_'):
                        try:
                            value = getattr(msg[0], attr)
                            if not callable(value):
                                print(f"  {attr}: {value} (type: {type(value)})")
                        except:
                            pass
            
            # 测试1.3: ws对象的方法
            print("\n[测试1.3] ws对象的方法...")
            print(f"ws对象类型: {type(ws)}")
            
            # 检查是否有send方法
            if hasattr(ws, 'send'):
                print(f"✅ 有send方法: {ws.send}")
                if hasattr(ws.send, '__doc__'):
                    print(f"send方法文档: {ws.send.__doc__}")
            else:
                print("❌ 没有send方法")
            
            # 检查是否有_send方法
            if hasattr(ws, '_send'):
                print(f"✅ 有_send方法: {ws._send}")
            else:
                print("❌ 没有_send方法")
            
            # 检查是否有其他发送方法
            send_methods = [attr for attr in dir(ws) if 'send' in attr.lower() and not attr.startswith('__')]
            print(f"所有包含'send'的方法: {send_methods}")
            
            # 测试1.4: 尝试访问底层连接（如果有）
            print("\n[测试1.4] 尝试访问底层连接...")
            for attr_name in ['_ws', 'ws', 'websocket', '_websocket', '_connection']:
                if hasattr(ws, attr_name):
                    ws_attr = getattr(ws, attr_name)
                    print(f"✅ 找到属性 {attr_name}: {type(ws_attr)}")
                    if hasattr(ws_attr, 'send'):
                        print(f"  底层连接有send方法: {ws_attr.send}")
                    send_methods_attr = [m for m in dir(ws_attr) if 'send' in m.lower() and not m.startswith('__')]
                    if send_methods_attr:
                        print(f"  底层连接包含'send'的方法: {send_methods_attr}")
            
            # 测试1.5: 查看ws对象的所有公共方法
            print("\n[测试1.5] ws对象的所有公共方法...")
            public_methods = [attr for attr in dir(ws) if not attr.startswith('_') and callable(getattr(ws, attr))]
            print(f"公共方法数量: {len(public_methods)}")
            print(f"前20个公共方法: {public_methods[:20]}")
            
            except Exception as e:
                print(f"❌ 测试过程中出错: {e}")
                import traceback
                traceback.print_exc()
            
            finally:
                # 清理：取消订阅
                try:
                    if msg and len(msg) > 0 and hasattr(msg[0], 'result') and isinstance(msg[0].result, int):
                        subscription_id = msg[0].result
                        if hasattr(ws, 'account_unsubscribe'):
                            await ws.account_unsubscribe(subscription_id)
                            print(f"\n已取消订阅: {subscription_id}")
                except Exception as e:
                    print(f"清理时出错（可忽略）: {e}")
                
                break  # 只测试一次
    except Exception as e:
        print(f"\n❌ 连接失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_solana_py_capabilities())
