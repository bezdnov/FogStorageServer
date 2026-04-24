from flask import Flask, request
from uuid import uuid4
from flask_socketio import SocketIO, emit
from threading import Lock
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = str(uuid4())

SHARDING_FACTOR = 2
REPLICATION_FACTOR = 1


socketio = SocketIO(app, logger=True, engineio_logger=True, async_mode='eventlet', max_http_buffer_size=16 * 1024 * 1024)
connected_users = {}
connection_lock = Lock()

# storage dict contains:
# size of shards kept by client on his machine
# size of shards saved by client in network
@socketio.on('connect')
def handle_join(storage_data):
    connection_id = request.sid
    connection_lock.acquire()
    connected_users[connection_id] = {**storage_data}
    connection_lock.release()
    emit('users_count', {'users_count': len(connected_users)})


@socketio.on('disconnect')
def handle_disconnect():
    connection_lock.acquire()
    del connected_users[request.sid]
    amount = len(connected_users)
    connection_lock.release()
    emit('users_count', {'users_count': amount})

# transmission of a shard
# shard_dict should contain this data
# (same as Shard.cs)
# {
#       FileAESKeyEncrypted
#       FilePublicKey
#       ShardIndex
#       ShardBytes
#       PoOBytesUnencrypted
#       PoOBytesEncrypted
#       MD5Checksum
#       ShardCheckLastTime
# }
# Saving algorithm:
# 1. Someone sends his shard
# 2. Server does request to the clients to understand who can be the best option;
# 3. Client saves file and sends "ok" when finished
@socketio.on('save_shard')
def handle_save_shard(shard_dict_string):
    sender_sid = request.sid

    # the string is a valid json
    shard_dict = json.loads(shard_dict_string)

    # transforming to list to sort
    connected_users_list = []
    with connection_lock:
        for user in connected_users.keys():
            connected_users_list.append((user, connected_users[user]['client_kept'], connected_users[user]['client_saved']))

    # the top of the sorted list are the ones who kept the least and saved the most data into the network;
    # they must be our choice
    connected_users_list.sort(key=lambda x: x[1] - x[2] * REPLICATION_FACTOR)

    saved_amount = 0
    for user, _, __ in connected_users_list:
        # we don't reuse the same user to store his data
        if user == sender_sid:
            continue

        # the has_shard is a "request" which is needed to check, if the client has some shard of file we're trying to save
        response = socketio.call('has_shard', {'FilePublicKey': shard_dict['FilePublicKey']}, to=user)

        # this already means that we can save the shard
        if not response:
            emit('save_shard', shard_dict, to=user)
            print('saving a shard to another user')

        saved_amount += 1

        # once we've saved to enough nodes, we can say that we've successfully saved a shard
        if saved_amount == REPLICATION_FACTOR:
            emit('shard_saved', {'FilePublicKey': shard_dict['FilePublicKey']}, to=sender_sid)
            break

    if saved_amount < REPLICATION_FACTOR:
        emit('save_shard_ack', {'response': 'saving shard failed: not enough peers', 'success': False}, to=sender_sid)
    else:
        emit('save_shard_ack', {'response': 'saved successfully', 'success': True}, to=sender_sid)


# This msg updates information on user
@socketio.on('update_saved_size')
def handle_saved_size(sizes_dict):
    sid = request.sid

    with connection_lock:
        connected_users[sid]['client_kept'] = sizes_dict['client_kept']
        connected_users[sid]['client_saved'] = sizes_dict['client_saved']

    emit('saved_size_ack', {'response': 'saved sizes updates', 'success': True}, to=sid)


# this mechanism is designed to proof that owner_sid is truly an owner of file file_public_key
# it makes him decrypt a small 16 bytes message; if it succeeds, True is returned, in any other case
# False is returned
# FIXME: in future its better to generate proof bytes here instead of asking checker_user to do so
def check_file_ownership(owner_sid, file_public_key) -> bool:
    with connection_lock:
        connected_user_sids = connected_users.keys()
    for checker_user in connected_user_sids:
       if checker_user != owner_sid:
           response = socketio.call('has_shard', {'FilePublicKey': file_public_key}, to=checker_user)
           if response:
               # generating random 16 bytes

                # response from client on this moment is base64 string
               enc_proof_bytes_b64, dec_proof_bytes_b64 = socketio.call(
                   'get_proof_bytes', {'FilePublicKey': file_public_key}, to=checker_user
               )[0]
               check_bytes = socketio.call(
                   'check_proof_bytes', {'FilePublicKey': file_public_key, 'ProofBytes': enc_proof_bytes_b64}, to=owner_sid
               )[0]
                # verdict: is the one who is owner is truly an owner?
               return dec_proof_bytes_b64 == check_bytes

    return False


# this "route" gets one shard and returns it to the one who requested it
# Input: {'FilePublicKey': hex, 'ShardIndex': int}
@socketio.on('get_shard')
def handle_get_shard(file_info):
    sid = request.sid

    file_public_key = file_info['FilePublicKey']
    shard_index = file_info['ShardIndex']

    is_owner = check_file_ownership(sid, file_public_key)

    if is_owner:
        with connection_lock:
            connected_users_list = connected_users.keys()

        for user in connected_users_list:
            response = socketio.call('has_shard', {'FilePublicKey': file_public_key, 'ShardIndex': shard_index}, to=sid)
            if response:
                emit('get_shard_ack', {'success': True, 'response': 'shard holder was found'}, to=sid)
                shard = socketio.call('get_shard', {'FilePublicKey': file_public_key, 'ShardIndex': shard_index}, to=sid)
                emit('receive_shard', shard, to=user)
                return

        socketio.emit('get_shard_ack', {'success': False, 'response': "shard holder wasn't found"})


@socketio.on('delete_file')
def handle_delete_file(file_public_key):
    sid = request.sid
    is_owner = check_file_ownership(sid, file_public_key)

    if is_owner:
        emit('delete_shard', file_public_key, broadcast=True)


@socketio.on('check_shard_status')
def handle_check_shard_status(file_public_key, index):
    count = 0
    for user in connected_users.keys():
        response = socketio.call('has_shard', {'FilePublicKey': file_public_key, 'ShardIndex': index}, to=user)
        if response:
            count += 1

    socketio.emit('check_shard_status', count, to=index)


if __name__ == '__main__':
    socketio.run(app, port=5000)


