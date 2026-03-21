from flask import Flask, request, jsonify
from uuid import uuid4
from flask_socketio import SocketIO, send, emit, join_room, leave_room, rooms
from time import sleep
from threading import Thread, Lock
from datetime import datetime, timedelta
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = str(uuid4())

SHARDING_FACTOR = 2
REPLICATION_FACTOR = 2


socketio = SocketIO(app, logger=True, engineio_logger=True, async_mode='eventlet')

connected_users = {}

# storage dict contains:
# size of shards kept by client on his machine
# size of shards saved by client in network
@socketio.on('connect')
def handle_join(storage_data):
    connection_id = request.sid
    connected_users[connection_id] = {**storage_data}
    emit('users_count', {'users_count': len(connected_users)})


@socketio.on('disconnect')
def handle_disconnect():
    del connected_users[request.sid]
    emit('users_count', {'users_count': len(connected_users)})

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
    for user in connected_users.keys():
        connected_users_list.append((user, connected_users[user]['client_kept'], connected_users[user]['client_saved']))


    # the top of the sorted list are the ones who kept the least and saved the most data into the network;
    # they must be our choice
    connected_users_list.sort(key=lambda x: x[1] - x[2] * REPLICATION_FACTOR)

    saved_amount = 0
    for user in connected_users_list:
        # we don't reuse the same user to store his data
        if user == sender_sid:
            continue

        # the has_shard is a "request" which is needed to check, if the client has some shard of file we're trying to save
        response = socketio.call('has_shard', {'FilePublicKey': shard_dict['FilePublicKey']}, to=user)

        # this already means that we can save the shard
        if not response['has_shard']:
            emit('save_shard', shard_dict, to=user)
            saved_amount += 1
            print('saving a shard to another user')

        # once we've saved to enough nodes, we can say that we've successfully saved a shard
        if saved_amount == REPLICATION_FACTOR:
            emit('shard_saved', to=sender_sid)
            break

    # "failed" case
    if saved_amount < REPLICATION_FACTOR:
        emit('save_shard_ack', {'response': 'saving shard failed: not enough peers'}, to=sender_sid)
    else:
        emit('save_shard_ack', {'response': 'saved successfully'}, to=sender_sid)


# receive
# we'll believe, that the user is honest, and he keeps size of shards he sent to the network
@socketio.on('saved_size')
def handle_saved_size(sizes_dict):
    guid = sizes_dict['guid']
    saved_in_network = sizes_dict['client_saved']
    saved_in_client = sizes_dict['client_kept']

    connected_users[guid]['client_kept'] = saved_in_client
    connected_users[guid]['client_saved'] = saved_in_network


# this "route" gets shards and returns it to request sender
@socketio.on('get_file')
def handle_get_file(get_file_data):
    pass


@socketio.on('delete_file')
def handle_delete_file():
    pass


@socketio.on('check_file')
def handle_check_file(file_data):
    print('here')
    emit('check_file', file_data)


if __name__ == '__main__':
    socketio.run(app)
