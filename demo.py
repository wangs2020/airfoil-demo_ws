import os
import torch
import random
import gradio as gr
from PIL import Image,ImageOps
from scipy.io.wavfile import write
import matplotlib.pyplot as plt
from audio_api import audio2parsec
from models import AE_AB,AE_A_variable,AE_B_Attention
from utils import get_name,get_params,get_path,get_point,point2img_new
from cquav.wing.airfoil import load_airfoils_collection
from cquav.wing.airfoil import Airfoil
from cquav.wing.profile import AirfoilSection
from cquav.wing.rect_console import RectangularWingConsole
import cadquery as cq
import numpy as np
from PIL import ImageDraw
import trimesh

nameDict = [
    '前缘半径',
    '上表面峰值',
    '下表面峰值',
    '后缘角'
]

param2idx = {name:i for i,name in enumerate(nameDict)}


airfoils_collection = load_airfoils_collection()
airfoil_data = airfoils_collection["NACA 6 series airfoils"]["NACA 64(3)-218 (naca643218-il)"]

airfoil = Airfoil(airfoil_data) # 将这个airfoil对象的profile属性，

modelA = AE_A_variable() # 编辑模型，input : source_keypoint,source_params,target_keypoint     output: target_params
modelB = AE_B_Attention() # 重建模型，input : target_keypoint,target_params   output: target_full
model = AE_AB(modelA,modelB) # input : source_keypoint,source_params,target_keypoint     output: target_full
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
checkpoint = torch.load('weights/logs_edit_AB/cond_ckpt_epoch_100.pth', map_location='cpu')
model.load_state_dict(checkpoint['model'], strict=True)
model.to(device)
model.eval()

img_paths = get_path('data/airfoil/demo/picked_uiuc_img')
point_paths = get_path('data/airfoil/demo/picked_uiuc')
name2params = get_params('data/airfoil/parsec_params.txt')
global_points = []
target_size = (400, 400)

def clear():
    global global_points
    global_points = []
    return None, None


def show_img(path):
    img = Image.open(path)
    padded_img = ImageOps.pad(img, target_size, method=Image.BOX, color=(0, 0, 0)) # Padding 黑色区域
    return  padded_img

def fn_before(idx):
    idx = (idx - 1)%len(img_paths)
    path = img_paths[int(idx)]
    img = show_img(path)
    return idx,img

def fn_sample(idx):
    idx = random.randint(0,len(img_paths)-1)
    path = img_paths[int(idx)]
    img = show_img(path)
    return idx,img


def fn_next(idx): 
    idx = (idx+1)%len(img_paths)
    path = img_paths[int(idx)]
    img = show_img(path)
    return idx,img

def process_audio(input_audio,slider0,slider1,slider2,slider3):
    print('process audio')
    rate, y = input_audio
    # 保存音频
    print('save audio')
    write("output2.wav", rate, y)
    name,strength = audio2parsec("output2.wav")
    if strength == -1:
        return name,slider0,slider1,slider2,slider3
    if name=='前缘半径':
        slider0 = strength
    elif name=='上表面峰值':
        slider1 = strength
    elif name=='下表面峰值':
        slider2 = strength
    elif name=='后缘角':
        slider3 = strength
    return name+'增加'+str(strength)+'倍',slider0,slider1,slider2,slider3

def prepare2airfoil(pred):
    # 上下表面
    upper = pred[:100][::4]
    upper[0][0],upper[0][1]=1,0
    mid = pred[100:101]
    mid[0][0],mid[0][1]=0,0
    low = pred[101:][::4]
    low[-1][0],low[-1][1]=1,0
    low[:,0] = upper[:,0][::-1]
   
    # 适配3D keypoint
    # 将上下表面和中点都concat在一起
    keypoint_3d = np.concatenate((upper,mid,low),axis=0)
    # print(keypoint_3d.shape)
    return keypoint_3d


@torch.no_grad()
def infer(input_image,idx,slider0,slider1,slider2,slider3):
    path = point_paths[int(idx)]
    params_slider = [slider0,slider1,slider2,slider3]
    ## ----- 首先，实现编辑物理量的逻辑 ----- ##
    data = get_point(path)
    source_params = torch.FloatTensor(name2params[get_name(path)])
    source_keypoint = data['keypoint'] # [20,2]                
    source_params = source_params.unsqueeze(-1) #[10,1]
    source_keypoint = source_keypoint.unsqueeze(0) # [1,20,2]
    source_params = source_params.unsqueeze(0) # [1,10,1]
    source_keypoint = source_keypoint.to(device) 
    source_params = source_params.to(device)
    target_params_pred,target_point_pred = model.editing_param(source_keypoint, source_params,params_slider)  
    point2img_new(target_point_pred[0].cpu().numpy())
    output_img = show_img('output.png')

    ## 算法拟合

    # ## ----- 其次，实现编辑控制点的逻辑 ----- ##
    if len(global_points)==2:
        point1 = global_points[0]
        point2 = global_points[1]
        point1 = torch.FloatTensor(point1).unsqueeze(0).unsqueeze(0).to(device)
        point2 = torch.FloatTensor(point2).unsqueeze(0).unsqueeze(0).to(device)
        target_params_pred,target_point_pred = model.editing_point(source_keypoint, source_params,point1,point2)
        point2img_new(target_point_pred[0].cpu().numpy())
        output_img = show_img('output.png')

    # 3D model
    # 处理得到 (51,)
    keypoint_3d = prepare2airfoil(target_point_pred[0].cpu().numpy())
    print(keypoint_3d.shape)
    print(keypoint_3d)
    airfoil_x = airfoil.profile['A'].copy()
    airfoil_y = airfoil.profile['B'].copy()
    try:
      airfoil.profile['A'] =keypoint_3d[:,0]  # (51,)
      airfoil.profile['B'] =keypoint_3d[:,1]  # (51,)
      airfoil_section = AirfoilSection(airfoil, chord=200)
      wing_console = RectangularWingConsole(airfoil_section, length=800)
      assy = cq.Assembly()
      assy.add(wing_console.foam, name="foam", color=cq.Color("lightgray"))
      assy.add(wing_console.front_box, name="left_box", color=cq.Color("yellow"))
      assy.add(wing_console.central_box, name="central_box", color=cq.Color("yellow"))
      assy.add(wing_console.rear_box, name="right_box", color=cq.Color("yellow"))
      assy.add(wing_console.shell, name="shell", color=cq.Color("lightskyblue2"))
      # show(assy, angular_tolerance=0.1)
      assy.save(path='output3d.stl',exportType='STL')
    except:
      airfoil.profile['A'] = airfoil_x  # (51,)
      airfoil.profile['B'] = airfoil_y  # (51,)
      airfoil_section = AirfoilSection(airfoil, chord=200)
      wing_console = RectangularWingConsole(airfoil_section, length=800)
      assy = cq.Assembly()
      assy.add(wing_console.foam, name="foam", color=cq.Color("lightgray"))
      assy.add(wing_console.front_box, name="left_box", color=cq.Color("yellow"))
      assy.add(wing_console.central_box, name="central_box", color=cq.Color("yellow"))
      assy.add(wing_console.rear_box, name="right_box", color=cq.Color("yellow"))
      assy.add(wing_console.shell, name="shell", color=cq.Color("lightskyblue2"))
      # show(assy, angular_tolerance=0.1)
      assy.save(path='output3d.stl',exportType='STL')
      # model3d = trimesh.load('output3d.stl')
    model3d =  gr.Model3D(value='output3d.stl',label='Output 3D Airfoil',camera_position=(270,0,None))
    return output_img,model3d

def get_points_with_draw(image, evt: gr.SelectData):
    x, y = evt.index[0], evt.index[1]
    point_radius, point_color = 5, (255, 255, 0)
    global global_points
    print((x, y))
    global_points.append([x, y])    
    # print(type(image)) #转成 PIL.Image
    image = Image.fromarray(image)
    # 创建一个可以在图像上绘图的对象
    draw = ImageDraw.Draw(image)
    draw.ellipse([(x - point_radius, y - point_radius), (x + point_radius, y + point_radius)], fill=point_color)

    # 得到两个点，需要画线 TODO （source -> target）

    return image
 

def reset(slider0,slider1,slider2,slider3):
    slider0  = 1
    slider1  = 1
    slider2  = 1
    slider3  = 1
    return slider0,slider1,slider2,slider3
   

quick_start_cn = """
        ## 快速开始
        1. 选择下方的airfoil example。
        2. 单击infer按钮， 对齐分辨率。
        3. 调整上方栏的Physical parameters，对机翼进行编辑。
        """
advanced_usage_cn = """
        ## 高级用法
        1. 使用大语言模型，语言转为文字，然后单击 `start audio to param` 。
        2. 单击 `Add Points` 添加关键点。
        3. 单击 `编辑灵活区域` 创建mask并约束未mask区域保持不变。
        """
# gr.themes.builder()

title = "# 机翼编辑软件"
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown(title)
    with gr.Column():
        with gr.Accordion("物理参数", open=False):
            with gr.Row():
              with gr.Column(min_width=200):
                img0 = gr.Image(value='assets/example_parsec_0.png',show_label=False,show_download_button=False)
                slider0 = gr.Slider(0, 10, step=0.1,label=nameDict[0],value=1)
              with gr.Column(min_width=200):
                img1 = gr.Image(value='assets/example_parsec_1.png',show_label=False,show_download_button=False)
                slider1 = gr.Slider(0, 10, step=0.1,label=nameDict[1],value=1)
              with gr.Column(min_width=200):
                img2 = gr.Image(value='assets/example_parsec_2.png',show_label=False,show_download_button=False)
                slider2 = gr.Slider(0, 10, step=0.1,label=nameDict[2],value=1)
              with gr.Column(min_width=200):
                img3 = gr.Image(value='assets/example_parsec_3.png',show_label=False,show_download_button=False)
                slider3 = gr.Slider(0, 10, step=0.1,label=nameDict[3],value=1)
            reset_param = gr.Button("reset")
            reset_param.click(reset,
                            inputs=[slider0,slider1,slider2,slider3],
                            outputs=[slider0,slider1,slider2,slider3])
        with gr.Accordion("语音控制", open=False):
            input_audio = gr.Audio(sources=["microphone","upload"],format="wav")
            with gr.Row():
              bn_param = gr.Button("start audio to param")
              paramTex = gr.Textbox()
            bn_param.click(process_audio,
                            inputs=[input_audio,slider0,slider1,slider2,slider3] ,
                            outputs=[paramTex,slider0,slider1,slider2,slider3])
    with gr.Row():
      # img = ImageMask()  # NOTE: hard image size code here.
      # img_out = gr.ImageEditor(label="Output Image")
      img_in = gr.Image(label="Input Airfoil",width=600,height=600)
      img_out = gr.Image(label="Output Airfoil",width=600,height=600)
      ## 3D model show
      # model3d = gr.Model3D(label='Output 3D Airfoil',value='assets/airfoil.stl',camera_position=(270,0,None)) # 调位姿
      model3d = gr.Model3D(label='Output 3D Airfoil',camera_position=(-90,2,600)) # 调位姿
    with gr.Row():
        with gr.Row():
            bn_before = gr.Button("前一个")
            bn_samlpe = gr.Button("随机")
            bn_next = gr.Button("后一个")
        with gr.Row():
            idx = gr.Number(value = 1,label='cur idx')
    # 新建一个button, 执行input_image，得到output_image
    img_in.select(get_points_with_draw, [img_in], img_in)
    with gr.Row():
        with gr.Column(scale=1, min_width=10):
            enable_add_points = gr.Button('加点')
        with gr.Column(scale=1, min_width=10):
            clear_points = gr.Button('清空')
        with gr.Column(scale=1, min_width=10):
            submit_button = gr.Button("生成")

    ## 编辑后物理参数的显示
    ips = [img_in,idx,slider0,slider1,slider2,slider3]
    bn_before.click(fn_before,
                    inputs=[idx],
                    outputs=[idx,img_in])
    bn_samlpe.click(fn_sample,
                inputs=[idx],
                outputs=[idx,img_in])
    bn_next.click(fn_next,
                  inputs=[idx],
                  outputs=[idx,img_in])
    clear_points.click(clear, outputs=[img_in,img_out])
    submit_button.click(infer,
                    inputs=ips,
                    outputs=[img_out,model3d])
    gr.Markdown("## Airfoil Examples")
    gr.Examples(
        examples=['data/airfoil/demo/picked_uiuc_img/ag36.png',
                  'data/airfoil/demo/picked_uiuc_img/ag37.png'],
        inputs=[img_in]
     )
    # Instruction
    with gr.Row():
        with gr.Column():
            quick_start_markdown = gr.Markdown(quick_start_cn)
        with gr.Column():
            advanced_usage_markdown = gr.Markdown(advanced_usage_cn)
if __name__=="__main__":
  demo.queue().launch(share=True)
  print('http://localhost:7860?__theme=dark')
  
